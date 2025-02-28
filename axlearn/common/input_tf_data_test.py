# Copyright © 2023 Apple Inc.

"""Tests tf.data inputs."""
# pylint: disable=no-self-use,too-many-lines
from typing import Dict, List, Optional, Sequence, Type, Union

import jax
import pytest
import seqio
import tensorflow as tf
import tensorflow_datasets as tfds
from absl.testing import absltest, parameterized

from axlearn.common import test_utils
from axlearn.common.config import config_for_function
from axlearn.common.input_fake import fake_serialized_json_source, fake_source, fake_text_source
from axlearn.common.input_tf_data import (
    BuildDatasetFn,
    DatasetToDatasetFn,
    _infer_num_examples,
    _infer_num_shards,
    _maybe_shard_examples,
    add_static_fields,
    batch,
    chain,
    concatenate_datasets,
    default_pad_example_fn,
    extract_from_sequence,
    identity,
    pack_to_batch,
    pad_to_batch,
    preserve_element_spec,
    ragged_to_tensor,
    rekey,
    remove_fields,
    sample_from_datasets,
    squeeze_fields,
    tfds_dataset,
    tfds_read_config,
    trim_and_pad_tensor,
    trim_to_batch,
    unpack,
    with_processor,
)


def build_ds_fn(
    is_training: bool, *, texts: Sequence[str], data_dir: Optional[str] = None
) -> BuildDatasetFn:
    del is_training, data_dir

    def ds_fn() -> tf.data.Dataset:
        def data_gen():
            for text in texts:
                yield text

        ds = tf.data.Dataset.from_generator(data_gen, output_types=tf.string)
        # Set the cardinality of the generated dataset.
        ds = ds.apply(tf.data.experimental.assert_cardinality(len(texts)))
        return ds

    return ds_fn


class SamplingTest(parameterized.TestCase):
    @parameterized.parameters(
        {"weights": [1.0, 0.0, 0.0], "expected": ["a", "b", "c", "d", "e"]},
        {"weights": [0.0, 1.0, 0.0], "expected": ["g", "h"]},
        {"weights": [0.0, 0.0, 1.0], "expected": ["w", "x", "y", "z"]},
    )
    def test_sampling_dataset_basic(self, weights, expected):
        sources = [
            config_for_function(build_ds_fn).set(
                texts=["a", "b", "c", "d", "e"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["g", "h"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["w", "x", "y", "z"],
            ),
        ]

        sampling_ds_cfg = config_for_function(sample_from_datasets).set(
            is_training=False,
            sources=sources,
            weights=weights,
        )
        ds_fn = sampling_ds_cfg.instantiate()

        actual = list(ds_fn().take(len(expected)))
        self.assertEqual(expected, actual)

        sources = [
            config_for_function(build_ds_fn).set(
                texts=["a", "b", "c"],
            ),
            config_for_function(build_ds_fn).set(
                texts=[],
            ),
            config_for_function(build_ds_fn).set(
                texts=["y", "z"],
            ),
        ]

        sampling_ds_cfg = config_for_function(sample_from_datasets).set(
            is_training=False,
            sources=sources,
            weights=weights,
        )
        ds_fn = sampling_ds_cfg.instantiate()

        # Dataset with zero cardinality.
        with self.assertRaises(ValueError):
            list(ds_fn().take(1))

    def test_sampling_dataset(self):
        tf.random.set_seed(1)
        sources = [
            config_for_function(build_ds_fn).set(
                texts=["a", "b", "c", "d", "e"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["g", "h"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["w", "x", "y", "z"],
            ),
        ]

        sampling_ds_cfg = config_for_function(sample_from_datasets).set(
            is_training=False,
            sources=sources,
            weights=[1 / 3, 1 / 3, 1 / 3],
            seed=1,
        )
        ds_fn = sampling_ds_cfg.instantiate()

        # Note that dataset ends when a dataset becomes empty.
        expected = ["a", "g", "w", "h", "b", "c", "d"]
        actual = [bytes.decode(x.numpy(), "utf-8") for x in ds_fn().take(len(expected))]
        assert expected == actual

        sources = [
            config_for_function(build_ds_fn).set(
                texts=["a", "b", "c", "d", "e"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["g", "h"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["w", "x", "y", "z"],
            ),
        ]

        sampling_ds_cfg = config_for_function(sample_from_datasets).set(
            is_training=False,
            sources=sources,
            weights=[0.0, 0.5, 0.5],
            seed=1,
        )
        ds_fn = sampling_ds_cfg.instantiate()

        expected = ["g", "w", "x", "h"]
        actual = [bytes.decode(x.numpy(), "utf-8") for x in ds_fn().take(len(expected))]
        assert expected == actual


class ConcatenateDatasetsTest(parameterized.TestCase):
    def test_raises_when_empty(self):
        with self.assertRaises(ValueError):
            concatenate_datasets(is_training=False, sources=[])

    def test_noop_for_one(self):
        sources = [
            config_for_function(build_ds_fn).set(
                texts=["a", "b", "c", "d", "e"],
            ),
        ]

        ds_fn = concatenate_datasets(is_training=False, sources=sources)

        expected = ["a", "b", "c", "d", "e"]
        actual = [bytes.decode(x.numpy(), "utf-8") for x in ds_fn()]
        assert expected == actual

    def test_concatenates_in_order(self):
        sources = [
            config_for_function(build_ds_fn).set(
                texts=["a", "b", "c", "d", "e"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["g", "h"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["w", "x", "y", "z"],
            ),
        ]

        ds_fn = concatenate_datasets(is_training=False, sources=sources)

        expected = ["a", "b", "c", "d", "e"] + ["g", "h"] + ["w", "x", "y", "z"]
        actual = [bytes.decode(x.numpy(), "utf-8") for x in ds_fn()]
        assert expected == actual


class TfdsTest(parameterized.TestCase):
    @parameterized.parameters(False, True)
    def test_tfds_read_config(self, is_training, read_parallelism=2, decode_parallelism=32):
        read_config = tfds_read_config(
            is_training=is_training,
            read_parallelism=read_parallelism,
            decode_parallelism=decode_parallelism,
        )
        self.assertEqual(read_config.input_context.num_input_pipelines, jax.process_count())
        self.assertEqual(read_config.input_context.input_pipeline_id, jax.process_index())
        if is_training:
            self.assertEqual(read_config.num_parallel_calls_for_decode, decode_parallelism)
            self.assertEqual(read_config.num_parallel_calls_for_interleave_files, read_parallelism)
            self.assertEqual(read_config.interleave_cycle_length, read_parallelism)
        else:
            self.assertEqual(read_config.num_parallel_calls_for_decode, 1)
            self.assertEqual(read_config.num_parallel_calls_for_interleave_files, 1)
            self.assertEqual(read_config.interleave_cycle_length, 1)

    @parameterized.parameters((1, 0), (16, 4))
    def test_tfds_read_config_with_custom_sharding(self, num_shards, shard_index):
        read_config = tfds_read_config(
            is_training=True,
            num_shards=num_shards,
            shard_index=shard_index,
        )
        self.assertEqual(read_config.input_context.num_input_pipelines, num_shards)
        self.assertEqual(read_config.input_context.input_pipeline_id, shard_index)

    @parameterized.parameters(
        ("train", 1024), ("validation", 8), ("train[:512]", 1), ("invalid", None)
    )
    @pytest.mark.gs_login  # must annotate within @parameterized.parameters
    def test_infer_num_shards(self, split: str, expected: Optional[int]):
        builder = tfds.builder("c4/en", try_gcs=True)
        self.assertEqual(_infer_num_shards(builder, split), expected)

    @parameterized.parameters(
        ("validation", 1043), ("test", 1063), ("test[:12]", 12), ("invalid", None)
    )
    @pytest.mark.gs_login  # must annotate within @parameterized.parameters
    def test_infer_num_examples(self, split: str, expected: Optional[int]):
        builder = tfds.builder("glue/cola:2.0.0", try_gcs=True)
        self.assertEqual(_infer_num_examples(builder, split), expected)

    @parameterized.parameters(
        ("validation", 5, True, "even split"),
        ("validation", 1044, False, "make copy for each host"),
        ("validation", 1044, True, "raise value error"),
        ("invalid", 5, True, "even split"),
    )
    @pytest.mark.gs_login
    def test_maybe_shard_examples(
        self, split: str, required_shards: int, is_training: bool, expected: str
    ):
        dataset_name = "glue/cola:2.0.0"
        builder = tfds.builder(dataset_name, try_gcs=True)
        read_config = config_for_function(tfds_read_config).set(is_training=is_training)
        if expected == "raise value error":
            with self.assertRaises(ValueError):
                _ = _maybe_shard_examples(
                    builder=builder,
                    read_config=read_config,
                    split=split,
                    required_shards=required_shards,
                    is_training=is_training,
                    dataset_name=dataset_name,
                )
        else:
            per_process_split = _maybe_shard_examples(
                builder=builder,
                read_config=read_config,
                split=split,
                required_shards=required_shards,
                is_training=is_training,
                dataset_name=dataset_name,
            )
            if expected == "even split":
                shard_index = read_config.shard_index or jax.process_index()
                expected_split = tfds.even_splits(split, n=required_shards, drop_remainder=False)[
                    shard_index
                ]
                self.assertTrue(expected_split == per_process_split)
            elif expected == "make copy for each host":
                self.assertTrue(per_process_split == split)

    @parameterized.parameters(
        ("validation", True, "sentence", "foobar"),
        ("test", True, "sentence", "barfoo"),
        ("validation", False, "sentence", "bar bar"),
        ("test", False, "sentence", "foo foo"),
    )
    @pytest.mark.gs_login
    def test_tfds_decoders(self, split: str, is_training: bool, field_name: str, expected: str):
        def tfds_custom_decoder() -> Dict[str, tfds.decode.Decoder]:
            @tfds.decode.make_decoder()
            def replace_field_value(field_value, _):
                return field_value + expected

            # pylint: disable=no-value-for-parameter
            return {field_name: replace_field_value()}

        decoders = config_for_function(tfds_custom_decoder)

        dataset_name = "glue/cola:2.0.0"
        source = config_for_function(tfds_dataset).set(
            dataset_name=dataset_name,
            split=split,
            is_training=is_training,
            shuffle_buffer_size=8 if is_training else 0,
            decoders=decoders,
        )
        ds = source.instantiate()

        for input_batch in ds().take(5):
            assert expected in input_batch[field_name].numpy().decode(
                "UTF-8"
            ), f"Missing {expected} string in {field_name} field"

    @parameterized.parameters(
        ("inputs_pretokenized"),
        ("prefix_ids"),
    )
    def test_tfds_decoders_ci(self, field_name: str):
        def tfds_custom_decoder() -> Dict[str, tfds.decode.Decoder]:
            @tfds.decode.make_decoder()
            def custom_fn(field_value, _):
                return field_value

            # pylint: disable=no-value-for-parameter
            return {field_name: custom_fn()}

        decoders = config_for_function(tfds_custom_decoder)
        custom_decoders = decoders.instantiate()
        assert isinstance(
            custom_decoders[field_name], tfds.decode.base.DecoderFn
        ), "The decoder fn is not of type tfds.decode.base.DecoderFn"


def _text_ds(texts: List[str], *, repeat=1) -> tf.data.Dataset:
    # TODO(markblee): consider de-duping these ds_fns.
    # pylint: disable=duplicate-code
    def data_gen():
        for _ in range(repeat):
            for index, text in enumerate(texts):
                yield {"text": text, "index": index, "is_valid": True}

    return tf.data.Dataset.from_generator(
        data_gen,
        output_signature={
            "text": tf.TensorSpec(shape=(), dtype=tf.string),
            "index": tf.TensorSpec(shape=(), dtype=tf.int32),
            "is_valid": tf.TensorSpec(shape=(), dtype=tf.bool),
        },
    )
    # pylint: enable=duplicate-code


class BatchTest(parameterized.TestCase):
    @parameterized.parameters(False, True)
    def test_padding(self, is_training):
        ds = _text_ds(["a", "b", "c"])
        ds = batch(
            global_batch_size=2, is_training=is_training, pad_example_fn=default_pad_example_fn
        )(ds)
        batch_index = 0
        for input_batch in ds:
            if is_training or batch_index == 0:
                self.assertSequenceEqual(input_batch["text"].numpy().tolist(), [b"a", b"b"])
                self.assertSequenceEqual(input_batch["index"].numpy().tolist(), [0, 1])
                self.assertSequenceEqual(input_batch["is_valid"].numpy().tolist(), [True, True])
            else:
                # The eval dataset will be padded by empty examples.
                self.assertSequenceEqual(input_batch["text"].numpy().tolist(), [b"c", b""])
                self.assertSequenceEqual(input_batch["index"].numpy().tolist(), [2, 0])
                self.assertSequenceEqual(input_batch["is_valid"].numpy().tolist(), [True, False])
            batch_index += 1
            if batch_index >= 10:
                break

    @parameterized.product(
        is_training=(False, True),
        prefetch_buffer_size=(32, None),
    )
    def test_prefetch_buffer_size(self, is_training, prefetch_buffer_size):
        ds = _text_ds(["a", "b", "c"])
        _ = batch(
            global_batch_size=2,
            is_training=is_training,
            pad_example_fn=default_pad_example_fn,
            prefetch_buffer_size=prefetch_buffer_size,
        )(ds)

    @parameterized.product(
        is_training=(False, True),
        post_batch_processor=(None, lambda x: x),
    )
    def test_post_batch_map_fn(self, is_training, post_batch_processor):
        ds = _text_ds(["a", "b", "c"])
        _ = batch(
            global_batch_size=2,
            is_training=is_training,
            pad_example_fn=default_pad_example_fn,
            post_batch_processor=post_batch_processor,
        )(ds)

    @parameterized.product(
        is_training=(False, True),
        repeat=(None, 1, 2),
    )
    def test_repeat(self, *, is_training, repeat):
        ds = _text_ds(["a", "b", "c"])
        ds = batch(
            global_batch_size=2,
            is_training=is_training,
            pad_example_fn=default_pad_example_fn,
            repeat=repeat,
        )(ds)
        batch_index = 0
        for input_batch in ds:
            if is_training or batch_index % 2 == 0:
                self.assertSequenceEqual(input_batch["text"].numpy().tolist(), [b"a", b"b"])
                self.assertSequenceEqual(input_batch["index"].numpy().tolist(), [0, 1])
            else:
                # The eval dataset will be padded by empty examples.
                self.assertSequenceEqual(input_batch["text"].numpy().tolist(), [b"c", b""])
                self.assertSequenceEqual(input_batch["index"].numpy().tolist(), [2, 0])
            batch_index += 1
            if batch_index >= 10:
                break
        if repeat is None:
            # Repeat indefinitely if is_training, otherwise do not repeat
            # (hence 2 batches after padding).
            self.assertEqual(10 if is_training else 2, batch_index)
        else:
            # If is_training, we discard remaining examples, hence one batch per epoch.
            # Otherwise we have two batches per epoch.
            self.assertEqual(repeat if is_training else 2 * repeat, batch_index)


class UnpackTest(test_utils.TestCase):
    # pylint: disable=no-self-use
    def _ds_fn(self) -> tf.data.Dataset:
        def nested_data_gen():
            for value in ["hello", "world"]:
                yield {"key1": "dummy", "key2": {"key3": {"key4": {"key5": value}}}}

        return tf.data.Dataset.from_generator(
            nested_data_gen,
            output_signature={
                "key1": tf.TensorSpec(shape=(), dtype=tf.string),
                "key2": {"key3": {"key4": {"key5": tf.TensorSpec(shape=(), dtype=tf.string)}}},
            },
        )

    def test_unpack_flattens_nested_path(self):
        ds = self._ds_fn()
        ds = unpack({"new_key2": ("key2", "key3", "key4", "key5"), "new_key1": ("key1",)})(ds)
        for el in ds:
            self.assertEqual(el["key1"], el["new_key1"])
            self.assertEqual(el["key2"]["key3"]["key4"]["key5"], el["new_key2"])


class RekeyTest(test_utils.TestCase):
    DEFAULT_VALUES = ["hello", "world"]

    def _ds_fn(self) -> tf.data.Dataset:
        def data_gen():
            for value in self.DEFAULT_VALUES:
                yield {"key1": value, "key2": value}

        return tf.data.Dataset.from_generator(
            data_gen,
            output_signature={
                "key1": tf.TensorSpec(shape=(), dtype=tf.string),
                "key2": tf.TensorSpec(shape=(), dtype=tf.string),
            },
        )

    def test_rekey_does_nothing_empty_keymap(self):
        ds = self._ds_fn()
        ds = rekey({})(ds)
        for ix, el in enumerate(ds):
            self.assertEqual(el["key1"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["key2"], self.DEFAULT_VALUES[ix])

    def test_rekey_maps_new_keys(self):
        ds = self._ds_fn()
        ds = rekey(
            {"new_key1": "key1", "new_key2": "key2", "new_key3": "key3"}, default_value="no"
        )(ds)
        for ix, el in enumerate(ds):
            self.assertEqual(set(el.keys()), {"new_key1", "new_key2", "new_key3"})
            self.assertEqual(el["new_key1"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["new_key2"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["new_key3"], "no")

    def test_rekey_changes_element_spec(self):
        ds = self._ds_fn()
        ds = rekey(
            {"new_key1": "key1", "new_key2": "key2", "new_key3": "key3"}, default_value="no"
        )(ds)
        expected = dict(
            new_key1=tf.TensorSpec(shape=(), dtype=tf.string),
            new_key2=tf.TensorSpec(shape=(), dtype=tf.string),
            new_key3=tf.TensorSpec(shape=(), dtype=tf.string),
        )
        self.assertNestedEqual(ds.element_spec, expected)

    def test_rekey_maps_falsey_reference_keys_to_default(self):
        ds = self._ds_fn()
        ds = rekey({"new_key1": "key1", "new_key2": None}, default_value="no")(ds)
        for ix, el in enumerate(ds):
            self.assertEqual(set(el.keys()), {"new_key1", "new_key2"})
            self.assertEqual(el["new_key1"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["new_key2"], "no")

    def test_rekey_maps_original_inputs_if_asked(self):
        ds = self._ds_fn()
        ds = rekey(
            {"new_key1": "key1", "new_key2": None}, default_value="no", retain_original_inputs=True
        )(ds)
        for ix, el in enumerate(ds):
            self.assertEqual(set(el.keys()), {"key1", "key2", "new_key1", "new_key2"})
            self.assertEqual(el["key1"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["key2"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["new_key1"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["new_key2"], "no")

    def test_rekey_does_not_map_missing_reference_keys_with_none_default(self):
        ds = self._ds_fn()
        ds = rekey(
            {"new_key1": "key1", "new_key2": "key2", "new_key3": "key3"}, default_value=None
        )(ds)
        for ix, el in enumerate(ds):
            self.assertEqual(set(el.keys()), {"new_key1", "new_key2"})
            self.assertEqual(el["new_key1"], self.DEFAULT_VALUES[ix])
            self.assertEqual(el["new_key2"], self.DEFAULT_VALUES[ix])

    def test_rekey_does_not_map_falsey_reference_keys_with_none_default(self):
        ds = self._ds_fn()
        ds = rekey({"new_key1": "key1", "new_key2": None}, default_value=None)(ds)
        for ix, el in enumerate(ds):
            self.assertEqual(set(el.keys()), {"new_key1"})
            self.assertEqual(el["new_key1"], self.DEFAULT_VALUES[ix])


class ProcessorsTest(parameterized.TestCase, tf.test.TestCase):
    def test_processor_for_sample_from_dataset(self):
        def process_fn(is_training: bool, *, add_token: str) -> DatasetToDatasetFn:
            del is_training

            @seqio.map_over_dataset
            def process_example_fn(example: str) -> str:
                example += add_token
                return example

            return process_example_fn

        tf.random.set_seed(1)
        source_cfgs = [
            config_for_function(build_ds_fn).set(
                texts=["a", "b", "c", "d", "e"],
            ),
            config_for_function(build_ds_fn).set(
                texts=["f", "g", "h"],
            ),
        ]
        processor_cfgs = [
            config_for_function(process_fn).set(add_token="_ds1"),
            config_for_function(process_fn).set(add_token="_ds2"),
        ]
        sources = [
            config_for_function(with_processor).set(
                source=ds_cfg,
                processor=processor_cfg,
                is_training=False,
            )
            for ds_cfg, processor_cfg in zip(source_cfgs, processor_cfgs)
        ]

        sampling_ds_cfg = config_for_function(sample_from_datasets).set(
            is_training=False,
            sources=sources,
            weights=[0.5, 0.5],
        )
        ds_fn = sampling_ds_cfg.instantiate()
        actual = list(ds_fn().take(2))

        expected = ["a_ds1", "f_ds2"]
        self.assertEqual(len(list(expected)), len(actual))
        for e, a in zip(expected, actual):
            self.assertAllEqual(e, a)

    def test_squeeze_fields(self):
        examples = [
            {
                "a": tf.constant([[1], [0]]),
                "b": tf.constant([[1], [1]]),
                "c": tf.constant([[[1, 2, 3]], [[4, 5, 6]]]),
                "d": tf.constant([[[[3, 2, 1]], [[6, 5, 4]]]]),
            }
        ]

        def gen():
            for ex in examples:
                yield ex

        ds = tf.data.Dataset.from_generator(
            gen,
            output_signature={
                "a": tf.TensorSpec(shape=(2, 1), dtype=tf.int32),
                "b": tf.TensorSpec(shape=(2, 1), dtype=tf.int32),
                "c": tf.TensorSpec(shape=(2, 1, 3), dtype=tf.int32),
                "d": tf.TensorSpec(shape=(1, 2, 1, 3), dtype=tf.int32),
            },
        )

        processor = (
            config_for_function(squeeze_fields).set(axis=dict(a=1, c=None, d=[0, 2])).instantiate()
        )
        ds = processor(ds)
        ds = list(ds.as_numpy_iterator())
        self.assertEqual(
            {
                "a": [1, 0],
                "b": [[1], [1]],
                "c": [[1, 2, 3], [4, 5, 6]],
                "d": [[3, 2, 1], [6, 5, 4]],
            },
            ds[0],
        )

    def test_remove_fields(self):
        examples = [
            {
                "a": tf.constant([1]),
                "b": tf.constant([[1], [1]]),
                "c": tf.constant([[2], [2]]),
            }
        ]

        def gen():
            for ex in examples:
                yield ex

        ds = tf.data.Dataset.from_generator(
            gen,
            output_signature={
                "a": tf.TensorSpec(shape=(1,), dtype=tf.int32),
                "b": tf.TensorSpec(shape=(2, 1), dtype=tf.int32),
                "c": tf.TensorSpec(shape=(2, 1), dtype=tf.int32),
            },
        )

        # Remove key does not exist in data.
        processor = config_for_function(remove_fields).set(fields=["d"]).instantiate()
        ds = processor(ds)
        ds_list = list(ds.as_numpy_iterator())
        self.assertEqual(
            {
                "a": [1],
                "b": [[1], [1]],
                "c": [[2], [2]],
            },
            ds_list[0],
        )
        # Remove a key in data.
        processor = config_for_function(remove_fields).set(fields=["c"]).instantiate()
        ds = processor(ds)
        ds_list = list(ds.as_numpy_iterator())
        self.assertEqual(
            {
                "a": [1],
                "b": [[1], [1]],
            },
            ds_list[0],
        )


class ExtractFromSequenceTest(parameterized.TestCase):
    COLOR_OPTIONS = ["blue", "green", "yellow", "black"]

    def _data_gen(self):
        def fn():
            yield dict(text="Which color would you like?", options=self.COLOR_OPTIONS)

        return tf.data.Dataset.from_generator(
            fn,
            output_signature={
                "text": tf.TensorSpec(shape=(), dtype=tf.string),
                "options": tf.TensorSpec(shape=(len(self.COLOR_OPTIONS),), dtype=tf.string),
            },
        )

    @parameterized.parameters(0, 1, 2, 3)
    def test_extract_single_index(self, idx: int = 0):
        out_key = "selected_option"
        ds = extract_from_sequence(in_key="options", out_key=out_key, idx=idx)(self._data_gen())
        el = next(iter(ds))
        self.assertEqual(el[out_key], self.COLOR_OPTIONS[idx])

    @parameterized.parameters(slice(0, 1), slice(0, 2), slice(1, 2))
    def test_extract_slice(self, slc: slice):
        out_key = "selected_options"
        ds = extract_from_sequence(in_key="options", out_key=out_key, idx=slc)(self._data_gen())
        el = next(iter(ds))
        self.assertSequenceEqual(
            [v.decode("utf8") for v in el[out_key].numpy()], self.COLOR_OPTIONS[slc]
        )


class PreserveElementSpecTest(parameterized.TestCase):
    def test_preserve_element_spec(self):
        @seqio.map_over_dataset
        def mapper(example):
            example["text"] = tf.py_function(func=lambda x: x, inp=example, Tout=tf.string)
            example["label"] = example["text"]
            return example

        orig_ds = _text_ds(["test"])

        # The mapper by default should produce an unknown shape.
        ds = mapper(orig_ds)
        self.assertEqual(ds.element_spec["text"].shape, tf.TensorShape(None))
        self.assertEqual(ds.element_spec["label"].shape, tf.TensorShape(None))

        # Mapping with preserve_element_spec should retain the spec.
        mapper = preserve_element_spec(mapper, key_map={"label": "text"})
        ds = mapper(orig_ds)
        self.assertEqual(ds.element_spec["text"].shape, tf.TensorShape(()))
        self.assertEqual(ds.element_spec["label"].shape, tf.TensorShape(()))


class WithProcessorTest(parameterized.TestCase):
    def test_with_processor(self):
        # Test that we can instantiate properly.
        ds_fn = with_processor(
            config_for_function(build_ds_fn).set(texts=["test"]),
            processor=config_for_function(identity),
            is_training=False,
        )
        next(iter(ds_fn()))

    def test_with_processor_optional_fields(self):
        # Test that we can instantiate properly.
        ds_fn = with_processor(
            # We deliberately use a fake source without is_training/data_dir params.
            config_for_function(fake_serialized_json_source).set(examples=[{"a": 1}, {"b": 2}]),
            processor=config_for_function(identity),
            is_training=False,
        )
        next(iter(ds_fn()))


class AddStaticFieldsTest(parameterized.TestCase):
    def test_add_static_fields(self):
        ds = fake_text_source(is_training=False)()
        processor = add_static_fields(key_map={"custom_key": "custom_value"})
        actual = processor(ds)

        expected = [
            {"text": tf.constant("eval text 0"), "custom_key": tf.constant("custom_value")},
            {"text": tf.constant("eval text 1"), "custom_key": tf.constant("custom_value")},
        ]
        self.assertEqual(
            actual.element_spec,
            {
                "text": tf.TensorSpec(shape=(), dtype=tf.string),
                "custom_key": tf.TensorSpec(shape=(), dtype=tf.string),
            },
        )
        self.assertSequenceEqual(expected, list(actual.as_numpy_iterator()))


class PadTest(parameterized.TestCase, tf.test.TestCase):
    @parameterized.parameters(
        dict(
            examples=[
                {"a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6]]), "b": tf.constant([1, 2])},
                {"a": tf.constant([[1, 2, 0]]), "b": tf.constant([3])},
            ],
            expected=[
                {
                    "a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6], [0, 0, 0], [0, 0, 0]]),
                    "b": tf.constant([1, 2, 0, 0, 0]),
                },
                {
                    "a": tf.constant([[1, 2, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]]),
                    "b": tf.constant([3, 0, 0, 0, 0]),
                },
            ],
        ),
    )
    def test_pad_to_batch(self, examples: Dict[str, tf.Tensor], expected: Dict[str, tf.Tensor]):
        processor = pad_to_batch(batch_size=5)
        source = fake_source(
            is_training=False,
            examples=examples,
            spec={
                "a": tf.TensorSpec(shape=[None, 3], dtype=tf.int32),
                "b": tf.TensorSpec(shape=[None], dtype=tf.int32),
            },
        )
        actual = list(processor(source()))
        tf.nest.map_structure(self.assertAllEqual, expected, actual)


class PackTest(parameterized.TestCase, tf.test.TestCase):
    @parameterized.parameters(
        dict(
            examples=[
                {"a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6]]), "b": tf.constant([1, 2])},
                {"a": tf.constant([[1, 2, 0]]), "b": tf.constant([3])},
                {"a": tf.constant([[3, 0, 0]]), "b": tf.constant([4])},
                {"a": tf.constant([[1, 2, 3], [4, 0, 0]]), "b": tf.constant([5, 6, 7, 8])},
            ],
            expected=[
                {
                    "a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6], [1, 2, 0], [3, 0, 0]]),
                    "b": tf.constant([1, 2, 3, 4, 0]),
                },
                {
                    "a": tf.constant([[1, 2, 3], [4, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]]),
                    "b": tf.constant([5, 6, 7, 8, 0]),
                },
            ],
        ),
        dict(
            examples=[
                {"a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6]]), "b": tf.constant([1, 2])},
                {"a": tf.constant([[1, 2, 0]]), "b": tf.constant([3, 4, 5, 6, 7])},
            ],
            expected=[
                {
                    "a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6], [0, 0, 0], [0, 0, 0]]),
                    "b": tf.constant([1, 2, 0, 0, 0]),
                },
                {
                    "a": tf.constant([[1, 2, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]]),
                    "b": tf.constant([3, 4, 5, 6, 7]),
                },
            ],
        ),
        # Test a case where each element is multi dimensional.
        dict(
            examples=[
                {"a": tf.ones([2, 2, 2], dtype=tf.int32)},
                {"a": tf.ones([3, 2, 2], dtype=tf.int32) * 2},
                {"a": tf.ones([3, 2, 2], dtype=tf.int32) * 3},
            ],
            expected=[
                {
                    "a": tf.concat(
                        [
                            tf.ones([2, 2, 2], dtype=tf.int32),
                            tf.ones([3, 2, 2], dtype=tf.int32) * 2,
                        ],
                        0,
                    ),
                },
                {
                    "a": tf.concat(
                        [
                            tf.ones([3, 2, 2], dtype=tf.int32) * 3,
                            tf.zeros([2, 2, 2], dtype=tf.int32),
                        ],
                        0,
                    ),
                },
            ],
            spec={"a": tf.TensorSpec(shape=[None, 2, 2], dtype=tf.int32)},
        ),
        # Test a case where an input element already exceeds batch_size.
        # We should raise in this case.
        dict(
            examples=[{"a": tf.ones([6, 2], dtype=tf.int32), "b": tf.constant([1])}],
            expected=tf.errors.InvalidArgumentError,
        ),
    )
    def test_pack_to_batch(
        self,
        examples: Sequence[Dict[str, tf.Tensor]],
        expected: Union[Type[Exception], Sequence[Dict[str, tf.Tensor]]],
        spec: Optional[Dict] = None,
    ):
        processor = pack_to_batch(batch_size=5)
        source = fake_source(
            is_training=False,
            examples=examples,
            spec=spec
            or {
                "a": tf.TensorSpec(shape=[None, None], dtype=tf.int32),
                "b": tf.TensorSpec(shape=[None], dtype=tf.int32),
            },
        )
        if isinstance(expected, list):
            actual = list(processor(source()))
            tf.nest.map_structure(self.assertAllEqual, expected, actual)
        else:
            with self.assertRaises(expected):
                list(processor(source()))

    @parameterized.parameters(
        dict(
            examples=[
                {"a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6]]), "b": tf.constant([1, 2])},
                {"a": tf.constant([[1, 2, 0]]), "b": tf.constant([3])},
                {"a": tf.constant([[3, 0, 0]]), "b": tf.constant([4])},
                {"a": tf.constant([[1, 2, 3], [4, 0, 0]]), "b": tf.constant([5, 6, 7, 8])},
            ],
            expected=[
                {
                    "a": tf.constant([[1, 0, 0], [2, 3, 0], [4, 5, 6], [1, 2, 0], [3, 0, 0]]),
                    "b": tf.constant([1, 2, 3, 4, 0]),
                },
                {
                    "a": tf.constant([[1, 2, 3], [4, 0, 0], [0, 0, 0], [0, 0, 0], [0, 0, 0]]),
                    "b": tf.constant([5, 6, 7, 8, 0]),
                },
            ],
        ),
        # Test a case where an input element already exceeds batch_size.
        # We should trim the batch in this case.
        dict(
            examples=[{"a": tf.ones([6, 3], dtype=tf.int32), "b": tf.ones([12], dtype=tf.int32)}],
            expected=[{"a": tf.ones([5, 3], dtype=tf.int32), "b": tf.ones([5], dtype=tf.int32)}],
        ),
    )
    def test_trim_and_pack_to_batch(
        self,
        examples: Sequence[Dict[str, tf.Tensor]],
        expected: Sequence[Dict[str, tf.Tensor]],
        spec: Optional[Dict] = None,
    ):
        processor = chain(trim_to_batch(batch_size=5), pack_to_batch(batch_size=5))
        source = fake_source(
            is_training=False,
            examples=examples,
            spec=spec
            or {
                "a": tf.TensorSpec(shape=[None, 3], dtype=tf.int32),
                "b": tf.TensorSpec(shape=[None], dtype=tf.int32),
            },
        )
        actual_ds = processor(source())
        actual = list(actual_ds)
        tf.nest.map_structure(self.assertAllEqual, expected, actual)
        expected_element_spec = {
            "a": tf.TensorSpec(shape=(5, 3), dtype=tf.int32, name=None),
            "b": tf.TensorSpec(shape=(5,), dtype=tf.int32, name=None),
        }
        tf.nest.map_structure(self.assertAllEqual, actual_ds.element_spec, expected_element_spec)


class ConvertRaggedTensorTest(parameterized.TestCase, tf.test.TestCase):
    @parameterized.parameters(
        dict(feature_shapes={"a": [None, 5]}), dict(feature_shapes={"a": [None, 5], "b": [5]})
    )
    def test_ragged_to_tensor(self, feature_shapes):
        examples = [
            {"a": tf.ragged.constant([[1, 2, 3]]), "b": tf.constant([5])},
            {"a": tf.ragged.constant([[1]]), "b": tf.constant([5])},
            {"a": tf.ragged.constant([[1, 2]]), "b": tf.constant([5])},
        ]
        ds = fake_source(is_training=False, examples=examples)()
        processor = ragged_to_tensor(feature_shapes=feature_shapes)
        actual = list(processor(ds))

        expected = [
            {"a": tf.constant([[1, 2, 3, 0, 0]]), "b": tf.constant([5])},
            {"a": tf.constant([[1, 0, 0, 0, 0]]), "b": tf.constant([5])},
            {"a": tf.constant([[1, 2, 0, 0, 0]]), "b": tf.constant([5])},
        ]
        tf.nest.map_structure(self.assertAllEqual, expected, actual)


class TrimAndPadTest(parameterized.TestCase):
    @parameterized.product(
        [
            {
                "max_len": 7,
                "expected_tensor": [
                    [3, 1, 0, 0, 0, 0, 0],
                    [3, 5, 4, 1, 0, 0, 0],
                    [3, 1, 6, 0, 0, 0, 0],
                ],
            },
            {
                "max_len": 5,
                "expected_tensor": [
                    [3, 1, 0, 0, 0],
                    [3, 5, 4, 1, 0],
                    [3, 1, 6, 0, 0],
                ],
            },
            {
                "max_len": 3,
                "expected_tensor": [
                    [3, 1, 0],
                    [3, 5, 4],
                    [3, 1, 6],
                ],
            },
        ],
        [
            {
                "input_tensor": tf.ragged.constant(
                    [
                        [3, 1],
                        [3, 5, 4, 1],
                        [3, 1, 6],
                    ]
                )
            },
            {
                "input_tensor": tf.constant(
                    [
                        [3, 1, 0, 0],
                        [3, 5, 4, 1],
                        [3, 1, 6, 0],
                    ]
                )
            },
        ],
    )
    def test_trim_and_pad_tensor(
        self,
        max_len: int,
        input_tensor: Union[tf.Tensor, tf.RaggedTensor],
        expected_tensor: tf.Tensor,
    ):
        t = trim_and_pad_tensor(input_tensor, max_len=max_len)
        tf.debugging.assert_equal(expected_tensor, t)

    @parameterized.parameters(
        {
            "input_tensor": tf.ragged.constant(
                [
                    [
                        [3, 1],
                        [3, 5, 4, 1],
                        [3, 1, 6],
                    ],
                    [
                        [3, 1, 2, 5],
                        [3, 5, 4, 1],
                        [3, 4],
                    ],
                ]
            ),
            "expected_tensor": [
                [
                    [3, 1, 0],
                    [3, 5, 4],
                    [3, 1, 6],
                ],
                [
                    [3, 1, 2],
                    [3, 5, 4],
                    [3, 4, 0],
                ],
            ],
        },
    )
    def test_trim_and_pad_tensor_nd(
        self, input_tensor: Union[tf.Tensor, tf.RaggedTensor], expected_tensor: tf.Tensor
    ):
        max_len = 3
        t = trim_and_pad_tensor(input_tensor, max_len=max_len)
        tf.debugging.assert_equal(expected_tensor, t)

    @parameterized.parameters(
        {
            "pad_id": -1,
            "max_len": 3,
            "input_tensor": tf.ragged.constant(
                [
                    [3, 1],
                    [3, 5, 4, 1],
                    [3, 1, 6],
                ]
            ),
            "expected_tensor": [
                [3, 1, -1],
                [3, 5, 4],
                [3, 1, 6],
            ],
        },
    )
    def test_trim_and_pad_non_zero_pad_id(
        self,
        pad_id: int,
        max_len: int,
        input_tensor: Union[tf.Tensor, tf.RaggedTensor],
        expected_tensor: tf.Tensor,
    ):
        t = trim_and_pad_tensor(input_tensor, max_len=max_len, pad_id=pad_id)
        tf.debugging.assert_equal(expected_tensor, t)


if __name__ == "__main__":
    absltest.main()
