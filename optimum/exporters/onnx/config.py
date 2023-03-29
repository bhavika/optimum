# coding=utf-8
# Copyright 2022 The HuggingFace Team. All rights reserved.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.
"""Common ONNX configuration classes that handle most of the features for building model specific configurations."""

from collections import OrderedDict
from pathlib import Path
from typing import TYPE_CHECKING, Any, Dict, Iterable, List, Optional, Tuple, Union

from transformers.utils import is_tf_available

from ...onnx import merge_decoders
from ...utils import (
    DummyAudioInputGenerator,
    DummyBboxInputGenerator,
    DummyInputGenerator,
    DummyPastKeyValuesGenerator,
    DummySeq2SeqDecoderTextInputGenerator,
    DummySeq2SeqPastKeyValuesGenerator,
    DummyTextInputGenerator,
    DummyVisionInputGenerator,
    is_diffusers_available,
    logging,
)
from .base import ConfigBehavior, OnnxConfig, OnnxConfigWithPast, OnnxSeq2SeqConfigWithPast
from .constants import ONNX_DECODER_MERGED_NAME, ONNX_DECODER_NAME, ONNX_DECODER_WITH_PAST_NAME


if TYPE_CHECKING:
    from transformers import PretrainedConfig, PreTrainedModel

    if is_tf_available():
        from transformers import TFPreTrainedModel

    if is_diffusers_available():
        from diffusers import ModelMixin

logger = logging.get_logger(__name__)


class TextEncoderOnnxConfig(OnnxConfig):
    """
    Handles encoder-based text architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (DummyTextInputGenerator,)


class TextDecoderOnnxConfig(OnnxConfigWithPast):
    """
    Handles decoder-based text architectures.
    """

    PAD_ATTENTION_MASK_TO_PAST = True
    DUMMY_INPUT_GENERATOR_CLASSES = (DummyTextInputGenerator, DummyPastKeyValuesGenerator)
    DUMMY_PKV_GENERATOR_CLASS = DummyPastKeyValuesGenerator

    @property
    def inputs(self) -> Dict[str, Dict[int, str]]:
        if self.use_past_in_inputs:
            common_inputs = {"input_ids": {0: "batch_size"}}
            self.add_past_key_values(common_inputs, direction="inputs")
            common_inputs["attention_mask"] = {0: "batch_size", 1: "past_sequence_length + 1"}
        else:
            common_inputs = {
                "input_ids": {0: "batch_size", 1: "sequence_length"},
                "attention_mask": {0: "batch_size", 1: "sequence_length"},
            }
        return common_inputs

    @property
    def outputs(self) -> Dict[str, Dict[int, str]]:
        if self.is_merged is False:
            common_outputs = super().outputs
        else:
            # in the merged case, we need to allow the `sequence_length` to be variable, as it is not 1
            # during the first pass without past key values
            common_outputs = OrderedDict({"logits": {0: "batch_size", 1: "sequence_length"}})
            self.add_past_key_values(common_outputs, direction="outputs")
        return common_outputs

    def post_process_exported_models(
        self,
        path: Path,
        models_and_onnx_configs: Dict[
            str, Tuple[Union["PreTrainedModel", "TFPreTrainedModel", "ModelMixin"], "OnnxConfig"]
        ],
        onnx_files_subpaths: List[str],
    ):
        # Attempt to merge only if the decoder-only was exported separately without/with past
        if self.use_past is True and len(models_and_onnx_configs) == 2:
            if onnx_files_subpaths is not None:
                decoder_path = Path(path, onnx_files_subpaths[0])
                decoder_with_past_path = Path(path, onnx_files_subpaths[1])
            else:
                decoder_path = Path(path, ONNX_DECODER_NAME + ".onnx")
                decoder_with_past_path = Path(path, ONNX_DECODER_WITH_PAST_NAME + ".onnx")
            decoder_merged_path = Path(path, ONNX_DECODER_MERGED_NAME + ".onnx")
            try:
                merge_decoders(
                    decoder=decoder_path,
                    decoder_with_past=decoder_with_past_path,
                    save_path=decoder_merged_path,
                )
            except Exception as e:
                raise Exception(f"Unable to merge decoders. Detailed error: {e}")

            # In order to do the validation of the two branches on the same file
            onnx_files_subpaths = [decoder_merged_path.name, decoder_merged_path.name]

            # We validate the two branches of the decoder model then
            models_and_onnx_configs[ONNX_DECODER_NAME][1].is_merged = True
            models_and_onnx_configs[ONNX_DECODER_NAME][1].use_cache_branch = False

            # Past key values won't be generated by default, but added in the input
            models_and_onnx_configs[ONNX_DECODER_NAME][1].use_past = False
            models_and_onnx_configs[ONNX_DECODER_NAME][1].use_past_in_inputs = True

            models_and_onnx_configs[ONNX_DECODER_WITH_PAST_NAME][1].use_cache_branch = True
            models_and_onnx_configs[ONNX_DECODER_WITH_PAST_NAME][1].is_merged = True

        return models_and_onnx_configs, onnx_files_subpaths


class TextSeq2SeqOnnxConfig(OnnxSeq2SeqConfigWithPast):
    """
    Handles encoder-decoder-based text architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (
        DummyTextInputGenerator,
        DummySeq2SeqDecoderTextInputGenerator,
        DummySeq2SeqPastKeyValuesGenerator,
    )

    @property
    def torch_to_onnx_input_map(self) -> Dict[str, str]:
        if self._behavior is ConfigBehavior.DECODER:
            return {
                "decoder_input_ids": "input_ids",
                "encoder_outputs": "encoder_hidden_states",
                "attention_mask": "encoder_attention_mask",
            }
        return {}

    @property
    def inputs(self) -> Dict[str, Dict[int, str]]:
        common_inputs = {}
        if self._behavior is not ConfigBehavior.DECODER:
            common_inputs["input_ids"] = {0: "batch_size", 1: "encoder_sequence_length"}

        common_inputs["attention_mask"] = {0: "batch_size", 1: "encoder_sequence_length"}

        if self._behavior is not ConfigBehavior.ENCODER:
            # TODO: it is likely this pop() is unwanted as we then always hit
            # https://github.com/huggingface/transformers/blob/v4.26.0/src/transformers/models/t5/modeling_t5.py#L965-L969
            common_inputs.pop("attention_mask")

            if self.use_past_in_inputs:
                # TODO: validate the axis name for attention_mask
                # common_inputs["attention_mask"][1] = "past_encoder_sequence_length + sequence_length"
                common_inputs["decoder_input_ids"] = {0: "batch_size"}
            else:
                common_inputs["decoder_input_ids"] = {0: "batch_size", 1: "decoder_sequence_length"}

            if self.use_past_in_inputs:
                self.add_past_key_values(common_inputs, direction="inputs")

        if self._behavior is ConfigBehavior.DECODER:
            common_inputs["encoder_outputs"] = {0: "batch_size", 1: "encoder_sequence_length"}

        return common_inputs

    def _create_dummy_input_generator_classes(self, **kwargs) -> List["DummyInputGenerator"]:
        dummy_text_input_generator = self.DUMMY_INPUT_GENERATOR_CLASSES[0](
            self.task, self._normalized_config, **kwargs
        )
        dummy_decoder_text_input_generator = self.DUMMY_INPUT_GENERATOR_CLASSES[1](
            self.task,
            self._normalized_config,
            **kwargs,
        )
        dummy_seq2seq_past_key_values_generator = self.DUMMY_INPUT_GENERATOR_CLASSES[2](
            self.task,
            self._normalized_config,
            encoder_sequence_length=dummy_text_input_generator.sequence_length,
            **kwargs,
        )
        dummy_inputs_generators = [
            dummy_text_input_generator,
            dummy_decoder_text_input_generator,
            dummy_seq2seq_past_key_values_generator,
        ]

        return dummy_inputs_generators


class VisionOnnxConfig(OnnxConfig):
    """
    Handles vision architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (DummyVisionInputGenerator,)


class TextAndVisionOnnxConfig(OnnxConfig):
    """
    Handles multi-modal text and vision architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (DummyTextInputGenerator, DummyVisionInputGenerator, DummyBboxInputGenerator)


class AudioOnnxConfig(OnnxConfig):
    """
    Handles audio architectures.
    """

    DUMMY_INPUT_GENERATOR_CLASSES = (DummyAudioInputGenerator,)

    @property
    def inputs(self) -> Dict[str, Dict[int, str]]:
        return {"input_values": {0: "batch_size", 1: "sequence_length"}}


class AudioToTextOnnxConfig(OnnxSeq2SeqConfigWithPast):
    DUMMY_INPUT_GENERATOR_CLASSES = (
        DummyAudioInputGenerator,
        DummySeq2SeqDecoderTextInputGenerator,
        DummySeq2SeqPastKeyValuesGenerator,
    )

    @property
    def inputs(self) -> Dict[str, Dict[int, str]]:
        common_inputs = {}

        if self._behavior is not ConfigBehavior.DECODER:
            common_inputs["input_features"] = {0: "batch_size", 1: "feature_size", 2: "encoder_sequence_length"}

        if self._behavior is not ConfigBehavior.ENCODER:
            if self.use_past_in_inputs:
                common_inputs["decoder_input_ids"] = {0: "batch_size"}
            else:
                common_inputs["decoder_input_ids"] = {0: "batch_size", 1: "decoder_sequence_length"}

            if self.use_past_in_inputs:
                self.add_past_key_values(common_inputs, direction="inputs")

        if self._behavior is ConfigBehavior.DECODER:
            common_inputs["encoder_outputs"] = {0: "batch_size", 1: "encoder_sequence_length"}

        return common_inputs

    @property
    def torch_to_onnx_input_map(self) -> Dict[str, str]:
        if self._behavior is ConfigBehavior.DECODER:
            return {
                "decoder_input_ids": "input_ids",
                "encoder_outputs": "encoder_hidden_states",
                "attention_mask": "encoder_attention_mask",
            }
        return {}


class EncoderDecoderOnnxConfig(OnnxSeq2SeqConfigWithPast):
    DUMMY_INPUT_GENERATOR_CLASSES = (DummyTextInputGenerator,)

    def __init__(
        self,
        config: "PretrainedConfig",
        task: str = "default",
        use_past: bool = False,
        use_past_in_inputs: Optional[bool] = None,
        use_present_in_outputs: Optional[bool] = None,
        behavior: ConfigBehavior = ConfigBehavior.MONOLITH,
    ):
        super().__init__(
            config,
            task=task,
            use_past=use_past,
            use_past_in_inputs=use_past_in_inputs,
            use_present_in_outputs=use_present_in_outputs,
            behavior=behavior,
        )

        from ..tasks import TasksManager

        self.is_decoder_with_past = False

        if self._behavior is not ConfigBehavior.DECODER:
            encoder_onnx_config_constructor = TasksManager.get_exporter_config_constructor(
                exporter="onnx", task="default", model_type=config.encoder.model_type
            )
            self._encoder_onnx_config = encoder_onnx_config_constructor(config.encoder)
            self._normalized_config.ENCODER_NORMALIZED_CONFIG_CLASS = self._encoder_onnx_config._normalized_config

        if self._behavior is not ConfigBehavior.ENCODER:
            decoder_onnx_config_constructor = TasksManager.get_exporter_config_constructor(
                exporter="onnx", task="default", model_type=config.decoder.model_type
            )
            kwargs = {}
            if issubclass(decoder_onnx_config_constructor.func, OnnxConfigWithPast):
                self.is_decoder_with_past = True
                kwargs["use_past"] = use_past
            else:
                self.use_present_in_outputs = False

            if use_past and not self.is_decoder_with_past:
                raise ValueError(
                    f"The decoder part of the encoder-decoder model is {config.decoder.model_type} which does not need "
                    "past key values."
                )

            self._decoder_onnx_config = decoder_onnx_config_constructor(config.decoder, **kwargs)
            self._normalized_config.DECODER_NORMALIZED_CONFIG_CLASS = self._decoder_onnx_config._normalized_config

            if isinstance(self._decoder_onnx_config, OnnxSeq2SeqConfigWithPast):
                self._past_key_values_generator = (
                    DummySeq2SeqDecoderTextInputGenerator,
                    DummySeq2SeqPastKeyValuesGenerator,
                )
            else:
                self._past_key_values_generator = (
                    DummySeq2SeqDecoderTextInputGenerator,
                    DummyPastKeyValuesGenerator,
                )

            self.DUMMY_INPUT_GENERATOR_CLASSES += self._past_key_values_generator

    @property
    def torch_to_onnx_input_map(self) -> Dict[str, str]:
        if self._behavior is ConfigBehavior.DECODER:
            return {
                "decoder_input_ids": "input_ids",
                "encoder_outputs": "encoder_hidden_states",
                "attention_mask": "encoder_attention_mask",
            }
        return {}

    def add_past_key_values(self, inputs_or_outputs: Dict[str, Dict[int, str]], direction: str):
        if self.is_decoder_with_past:
            return self._decoder_onnx_config.add_past_key_values(inputs_or_outputs, direction)

    def flatten_past_key_values(self, flattened_output, name, idx, t):
        if self.is_decoder_with_past:
            return self._decoder_onnx_config.flatten_past_key_values(flattened_output, name, idx, t)

    def flatten_output_collection_property(self, name: str, field: Iterable[Any]) -> Dict[str, Any]:
        return self._decoder_onnx_config.flatten_output_collection_property(name, field)

    def generate_dummy_inputs_for_validation(self, reference_model_inputs: Dict[str, Any]) -> Dict[str, Any]:
        if self._behavior is ConfigBehavior.ENCODER:
            return self._encoder_onnx_config.generate_dummy_inputs_for_validation(reference_model_inputs)
        else:
            if self._behavior is ConfigBehavior.DECODER:
                reference_model_inputs["input_ids"] = reference_model_inputs.pop("decoder_input_ids")

                # for encoder-decoder custom models, always pass encoder_hidden_states as input
                reference_model_inputs["encoder_hidden_states"] = reference_model_inputs.pop("encoder_outputs")[0]

            return self._decoder_onnx_config.generate_dummy_inputs_for_validation(reference_model_inputs)

    def post_process_exported_models(
        self,
        path: Path,
        models_and_onnx_configs: Dict[
            str, Tuple[Union["PreTrainedModel", "TFPreTrainedModel", "ModelMixin"], "OnnxConfig"]
        ],
        onnx_files_subpaths: List[str],
    ):
        models_and_onnx_configs, onnx_files_subpaths = super().post_process_exported_models(
            path, models_and_onnx_configs, onnx_files_subpaths
        )
        if self.use_past is True and len(models_and_onnx_configs) == 3:
            models_and_onnx_configs[ONNX_DECODER_NAME][1]._decoder_onnx_config.is_merged = True
            models_and_onnx_configs[ONNX_DECODER_NAME][1]._decoder_onnx_config.use_cache_branch = False

            # Past key values won't be generated by default, but added in the input
            models_and_onnx_configs[ONNX_DECODER_NAME][1]._decoder_onnx_config.use_past = False
            models_and_onnx_configs[ONNX_DECODER_NAME][1]._decoder_onnx_config.use_past_in_inputs = True

            models_and_onnx_configs[ONNX_DECODER_WITH_PAST_NAME][1]._decoder_onnx_config.use_cache_branch = True
            models_and_onnx_configs[ONNX_DECODER_WITH_PAST_NAME][1]._decoder_onnx_config.is_merged = True

        return models_and_onnx_configs, onnx_files_subpaths
