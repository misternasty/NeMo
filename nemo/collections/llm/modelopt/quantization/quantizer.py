# Copyright (c) 2024, NVIDIA CORPORATION.  All rights reserved.
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

import os
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import TYPE_CHECKING, Optional, Union

import torch
from accelerate.hooks import remove_hook_from_module
from datasets import load_dataset
from tqdm import tqdm

from nemo.collections import llm
from nemo.collections.llm.inference import MCoreTokenizerWrappper, generate
from nemo.collections.llm.utils import barrier, torch_dtype_from_precision
from nemo.lightning.ckpt_utils import ckpt_to_context_subdir
from nemo.lightning.io.pl import TrainerContext, ckpt_to_weights_subdir
from nemo.utils import logging
from nemo.utils.get_rank import is_global_rank_zero
from nemo.utils.import_utils import safe_import
from nemo.utils.model_utils import unwrap_model

if TYPE_CHECKING:
    from nemo.lightning import Trainer
    from nemo.lightning.megatron_parallel import MegatronParallel

_, HAVE_MODELOPT = safe_import("modelopt")
if HAVE_MODELOPT:
    import modelopt.torch.quantization as mtq
    from modelopt.torch.export import export_hf_checkpoint, export_tensorrt_llm_checkpoint

    QUANT_CFG_CHOICES = {
        "int8": mtq.INT8_DEFAULT_CFG,
        "int8_sq": mtq.INT8_SMOOTHQUANT_CFG,
        "fp8": mtq.FP8_DEFAULT_CFG,
        "int4_awq": mtq.INT4_AWQ_CFG,
        "w4a8_awq": mtq.W4A8_AWQ_BETA_CFG,
        "int4": mtq.INT4_BLOCKWISE_WEIGHT_ONLY_CFG,
        "nvfp4": mtq.NVFP4_DEFAULT_CFG,
    }

SUPPORTED_DTYPE = [16, "16", "bf16"]  # Default precision for non-quantized layers
SUPPORTED_EXPORT_FMT = ["trtllm", "nemo", "hf"]


@dataclass
class QuantizationConfig:
    """Quantization parameters.

    Available quantization methods are listed in `QUANT_CFG_CHOICES` dictionary above.
    Please consult Model Optimizer documentation https://nvidia.github.io/TensorRT-Model-Optimizer/ for details.

    Quantization algorithm can also be conveniently set to None to perform only weights export step
    for TensorRT-LLM deployment. This is useful to getting baseline results for a full-precision model.
    """

    algorithm: Optional[str] = "fp8"
    awq_block_size: int = 128
    sq_alpha: float = 0.5
    enable_kv_cache: Optional[bool] = None

    calibration_dataset: str = "cnn_dailymail"
    calibration_dataset_size: int = 512
    calibration_batch_size: int = 64
    calibration_seq_len: int = 128


@dataclass
class ExportConfig:
    """Inference configuration for the quantized TensorRT-LLM checkpoint.

    Available export formats methods are listed in `SUPPORTED_EXPORT_FMT` dictionary above.
    """

    path: str  # TODO: In fact `Union[Path, str]` but NeMo-Run CLI fails on type hint: unserializable PosixPath value
    export_format: str = "trtllm"
    dtype: Union[str, int] = "bf16"
    decoder_type: Optional[str] = None
    inference_tp: int = 1
    inference_pp: int = 1
    generate_sample: bool = False

    def __post_init__(self):
        self.path = Path(self.path)


class Quantizer:
    """Post-training quantization (PTQ) and export of NeMo 2.0 checkpoints.

    PTQ converts selected model layers to low-precision format (e.g., INT4, FP8) for efficient serving.
    The process consist of several steps:

        1. Loading a Nemo model from disk using appropriate parallelism strategy
        2. Calibrating the model to obtain appropriate algorithm-specific scaling factors
        3. Producing an output directory with a quantized checkpoint and a tokenizer

    By default, the output directory produced is intended to be consumed by TensorRT-LLM toolbox
    for efficient inference. This can be achieved using nemo.export.tensorrt_llm module.
    This can be changed to export a standard NeMo 2.0 checkpoint instead using `ExportConfig`.
    """

    def __init__(self, quantization_config: QuantizationConfig, export_config: ExportConfig):
        """Initialize Quantizer with quantization and export configurations."""
        if not HAVE_MODELOPT:
            raise RuntimeError("nvidia-modelopt is needed to use Quantizer")
        if not torch.cuda.is_available():
            raise EnvironmentError("GPU is required for the quantization.")

        self.quantization_config = quantization_config
        self.export_config = export_config

        algorithm = quantization_config.algorithm
        dtype = export_config.dtype
        # Export and Quantization config sanity checks
        assert algorithm is None or algorithm in QUANT_CFG_CHOICES, f"Unsupported quantization algorithm: {algorithm}"
        if export_config is not None:
            assert dtype in SUPPORTED_DTYPE, f"Unsupported export dtype: {dtype}"
        self.torch_dtype = torch_dtype_from_precision(dtype)

    @staticmethod
    def _setup(model) -> None:
        """Setup model for quantization."""
        if isinstance(model, llm.HFAutoModelForCausalLM):
            return
        # TODO: disable activation checkpointing
        model.config.vocab_size = model.tokenizer.vocab_size
        model.freeze()

    def _get_decoder_type(self, model):
        if self.export_config.decoder_type is not None:
            return self.export_config.decoder_type

        unwrapped_model = model
        while not isinstance(unwrapped_model, (llm.GPTModel, llm.HFAutoModelForCausalLM)):
            unwrapped_model = unwrapped_model.module

        if decoder_type := get_modelopt_decoder_type(unwrapped_model):
            return decoder_type
        raise ValueError(
            "Could not infer the decoder type for the provided model. "
            "Please provide the decoder type explicitly in the ExportConfig."
        )

    @staticmethod
    def _generate_sample(model):
        prompts = ["Born in north-east France, Soyer trained as a", "Born in California, Soyer trained as a"]

        outputs = []
        if isinstance(model, llm.HFAutoModelForCausalLM):
            for prompt in prompts:
                input_ids = model.tokenizer.tokenizer(prompt, return_tensors="pt")
                input_ids = {k: v.to(model.model.device) for k, v in input_ids.items()}
                output = model.model.generate(**input_ids, max_new_tokens=30)
                decoded = model.tokenizer.tokenizer.decode(output[0], skip_special_tokens=True)
                outputs.append(decoded)
        else:
            mcore_tokenizer = MCoreTokenizerWrappper(model.tokenizer)
            mcore_inference = model.get_inference_wrapper(
                params_dtype=torch.bfloat16, inference_batch_times_seqlen_threshold=30
            )

            generated = [r.generated_text for r in generate(mcore_inference, mcore_tokenizer, prompts)]
            outputs = [prompt + generation for prompt, generation in zip(prompts, generated)]

        logging.info(f"Sample generation after PTQ (with prompts): {outputs}")

    def _get_forward_loop(self, model):
        get_dataloader = create_data_iterator_getter(
            model,
            dataset=self.quantization_config.calibration_dataset,
            seq_len=self.quantization_config.calibration_seq_len,
            batch_size=self.quantization_config.calibration_batch_size,
            calibration_size=self.quantization_config.calibration_dataset_size,
        )
        number_of_batches = (
            self.quantization_config.calibration_dataset_size // self.quantization_config.calibration_batch_size
        )

        if isinstance(model, llm.HFAutoModelForCausalLM):
            device = model.model.device

            def huggingface_forward_loop(model):
                dataloader = get_dataloader()
                for batch in dataloader:
                    model(batch.to(device))

            return huggingface_forward_loop

        return self.create_megatron_forward_loop(
            get_dataloader,
            num_batches=number_of_batches,
            seq_length=self.quantization_config.calibration_seq_len,
            micro_batch_size=self.quantization_config.calibration_batch_size,
        )

    def quantize(self, model: "MegatronParallel", forward_loop=None):
        """Quantize the model and calibrate using given forward loop.

        If forward_loop is not provided, a forward loop will be created using the calibration dataset.
        """
        if forward_loop is None:
            forward_loop = self._get_forward_loop(model)

        algorithm = self.quantization_config.algorithm
        if algorithm is None:
            logging.info("Quantization algorithm set to None, returning the non-quantized model")
            return model

        logging.info(f"Quantizing model to {algorithm}...")

        self._setup(model)
        decoder_type = self._get_decoder_type(model)
        quant_cfg = QUANT_CFG_CHOICES[algorithm]
        if "awq" in algorithm:
            weight_quantizer = quant_cfg["quant_cfg"]["*weight_quantizer"]
            if isinstance(weight_quantizer, list):
                weight_quantizer = weight_quantizer[0]
            weight_quantizer["block_sizes"][-1] = self.quantization_config.awq_block_size

        # Always turn on FP8 kv cache to save memory footprint.
        # For int8_sq, we use int8 kv cache.
        # TODO: Investigate why enabling FP8 kv cache will cause accuracy regressions for Nemotron.
        enable_quant_kv_cache = self.quantization_config.enable_kv_cache
        if enable_quant_kv_cache is None:
            enable_quant_kv_cache = "int8" not in algorithm and decoder_type != "gpt"
        logging.info(f"{'Enabled' if enable_quant_kv_cache else 'Disabled'} KV cache quantization")
        quant_cfg["quant_cfg"]["*output_quantizer"] = {
            "num_bits": 8 if algorithm == "int8_sq" else (4, 3),
            "axis": None,
            "enable": enable_quant_kv_cache,
        }
        if algorithm == "int8_sq":
            sq_alpha = self.quantization_config.sq_alpha
            logging.info(f"Using int8_sq alpha = {sq_alpha}")
            quant_cfg["algorithm"] = {"method": "smoothquant", "alpha": sq_alpha}

        unwrapped_model = mtq.quantize(unwrap_for_modelopt_operations(model), quant_cfg, forward_loop)
        if decoder_type == "gpt":
            # We found squared_relu may have an under-calibration problem.
            # Clamp the scaling_factor with a min threshold to avoid under-calibration.
            match algorithm:
                case "fp8":
                    maxbound = 448
                case "int8_sq":
                    maxbound = 127
                case _:
                    maxbound = 0

            unwrapped_model = mtq.postprocess_amax(
                unwrapped_model, "*input_quantizer", lambda amax: torch.clamp(amax, min=0.01 * maxbound)
            )

        if is_global_rank_zero():
            mtq.print_quant_summary(unwrapped_model)

        if self.export_config.generate_sample:
            logging.info("Generating a sample output after model quantization.")
            self._generate_sample(model)

        return model

    def create_megatron_forward_loop(
        self, get_dataloader, num_batches, seq_length=None, micro_batch_size=None, decoder_seq_length=None
    ):
        """Create a forward loop for over a given data iterator."""
        from megatron.core.pipeline_parallel.schedules import get_forward_backward_func

        forward_backward_func = get_forward_backward_func()

        def forward_step_func(data_iterator, model):
            data = next(data_iterator)
            batch_len, seq_len = data.shape
            position_ids = torch.arange(seq_len, device=data.device).expand((batch_len, seq_len))
            output_tensor = model(data, position_ids, None)

            def _mock_loss_function(tensor):
                return 0, {}

            return output_tensor, _mock_loss_function

        def loop(model):
            dataloader = get_dataloader()
            forward_backward_func(
                forward_step_func=forward_step_func,
                data_iterator=dataloader,
                model=model,
                num_microbatches=num_batches,
                seq_length=seq_length,
                micro_batch_size=micro_batch_size,
                decoder_seq_length=decoder_seq_length,
                forward_only=True,
            )

        return loop

    @staticmethod
    def _validate_quantized_checkpoint(checkpoint_dir: Path, tensor_parallelism_size: int) -> bool:
        """Basic validation of the model structure."""
        saved_config = (checkpoint_dir / "config.json").exists()
        saved_weights = True
        for i in range(tensor_parallelism_size):
            saved_weights &= (checkpoint_dir / f"rank{i}.safetensors").exists()

        export_successful = saved_config and saved_weights
        if not export_successful:
            logging.error("Failed to export the quantized model.")
        return export_successful

    def _save_tokenizer(self, model, model_dir: str, export_dir: Path, export_fmt: str):
        if not is_global_rank_zero() or export_fmt == "nemo":
            # For NeMo model format, the tokenizer is saved via trainer.save_checkpoint()
            return

        is_automodel = isinstance(model, llm.HFAutoModelForCausalLM)
        if is_automodel:
            if export_fmt != "hf":
                export_dir = export_dir / "huggingface_tokenizer"
            model.tokenizer.save_pretrained(str(export_dir))
        else:
            # Save the model context in order to restore its tokenizer later. The destination
            # path is "nemo_context" as this name is used in nemo.export to setup tokenizer.
            shutil.copytree(
                ckpt_to_context_subdir(model_dir), os.path.join(export_dir, "nemo_context"), dirs_exist_ok=True
            )

    def export(self, model, model_dir: str, trainer: Optional["Trainer"] = None) -> None:
        """Export model to a TensorRT-LLM or NeMo checkpoint."""
        export_dir = self.export_config.path
        export_fmt = self.export_config.export_format
        assert export_fmt in SUPPORTED_EXPORT_FMT, f"Unsupported export format: {export_fmt}"
        is_automodel = isinstance(model, llm.HFAutoModelForCausalLM)

        # Standard NeMo 2.0 checkpoint format
        if self.export_config.export_format == "nemo":
            assert (
                not is_automodel
            ), "NeMo export format can only be used with native NeMo checkpoints, not HuggingFace models"
            assert trainer is not None, "Trainer required for NeMo export."
            trainer.save_checkpoint(export_dir)
            barrier()
            if is_global_rank_zero():
                TrainerContext.from_trainer(trainer).io_dump(ckpt_to_context_subdir(export_dir), yaml_attrs=["model"])
                assert (Path(ckpt_to_weights_subdir(export_dir, False)) / "modelopt_state").exists()
        elif self.export_config.export_format == "hf":
            assert is_automodel, "HF export is only supported for AutoModelForCausalLM"
            unwrapped_model = unwrap_for_modelopt_operations(model)
            with torch.inference_mode():
                export_hf_checkpoint(
                    unwrapped_model,
                    export_dir=export_dir,
                )
        # TRT-LLM
        else:
            inference_tp = self.export_config.inference_tp
            inference_pp = self.export_config.inference_pp
            use_nfs_workspace = (not is_automodel) and (model.config.pipeline_model_parallel_size > 1)

            with torch.inference_mode():
                remove_hook_from_module(model, recurse=True)
                export_tensorrt_llm_checkpoint(
                    model=unwrap_for_modelopt_operations(model),
                    decoder_type=self._get_decoder_type(model),
                    dtype=self.torch_dtype,
                    export_dir=export_dir,
                    inference_tensor_parallel=inference_tp,
                    inference_pipeline_parallel=inference_pp,
                    use_nfs_workspace=use_nfs_workspace,
                )
            barrier()
            if is_global_rank_zero():
                assert self._validate_quantized_checkpoint(export_dir, inference_tp)

        if is_global_rank_zero():
            self._save_tokenizer(model, model_dir, export_dir, export_fmt)
            logging.info(f"Export succeeded, model has been exported to {export_dir}.")


def get_calib_data_iter(
    data: str = "cnn_dailymail", batch_size: int = 64, calib_size: int = 512, max_sequence_length: int = 512
):
    """Creates a sample data iterator for calibration."""
    if data == "wikitext":
        dataset = load_dataset("wikitext", "wikitext-103-v1", split="train")
        text_column = "text"
    elif data == "cnn_dailymail":
        dataset = load_dataset("cnn_dailymail", name="3.0.0", split="train")
        text_column = "article"
    else:
        # Assume a local JSON dataset with a column named "text"
        dataset = load_dataset("json", data_files=data, split="train")
        text_column = "text"
    calib_size = max(min(len(dataset), calib_size), batch_size)
    for i in range(calib_size // batch_size):
        batch = dataset[i * batch_size : (i + 1) * batch_size][text_column]
        for j in range(len(batch)):
            batch[j] = batch[j][:max_sequence_length]
        yield batch


def create_data_iterator_getter(model, dataset, seq_len, batch_size, calibration_size):
    """Create a function that provides iterator over a given dataset."""

    def _get_iterator():
        CHARACTERS_PER_TOKEN = 4

        dataloader = get_calib_data_iter(
            data=dataset,
            max_sequence_length=CHARACTERS_PER_TOKEN * seq_len,
            batch_size=batch_size,
            calib_size=calibration_size,
        )

        data = []
        for batch in dataloader:
            batch = [model.tokenizer.text_to_ids(text)[:seq_len] for text in batch]
            batch = [ids + (seq_len - len(ids)) * [model.tokenizer.eos] for ids in batch]
            data.append(torch.tensor(batch, device=model.device))

        return iter(tqdm(data))

    return _get_iterator


huggingface_model_type_pattern_match = {
    "GPT2": "gpt",
    "Mllama": "mllama",
    "Llama": "llama",
    "Mistral": "llama",
    "GPTJ": "gptj",
    "FalconForCausalLM": "falcon",
    "RWForCausalLM": "falcon",
    "baichuan": "baichuan",
    "MPT": "mpt",
    "Bloom": "bloom",
    "ChatGLM": "chatglm",
    "QWen": "qwen",
    "RecurrentGemma": "recurrentgemma",
    "Gemma2": "gemma2",
    "Gemma": "gemma",
    "phi3small": "phi3small",
    "phi3": "phi3",
    "PhiMoEForCausalLM": "phi3",
    "phi": "phi",
    "TLGv4ForCausalLM": "phi",
    "MixtralForCausalLM": "llama",
    "ArcticForCausalLM": "llama",
    "StarCoder": "gpt",
    "Dbrx": "dbrx",
    "T5": "t5",
    "Bart": "bart",
    "GLM": "glm",
    "InternLM2ForCausalLM": "internlm",
    "ExaoneForCausalLM": "exaone",
    "Nemotron": "gpt",
    "Deepseek": "deepseek",
    "Whisper": "whisper",
}

gpt_model_type = [
    (llm.Baichuan2Model, "baichuan"),
    (llm.ChatGLMModel, "chatglm"),
    (llm.Gemma2Model, "gemma2"),
    (llm.GemmaModel, "gemma"),
    (llm.LlamaModel, "llama"),
    (llm.MistralModel, "llama"),
    (llm.MixtralModel, "llama"),
    (llm.NemotronModel, "gpt"),
    (llm.Qwen2Model, "qwen"),
    (llm.StarcoderModel, "gpt"),
    (llm.Starcoder2Model, "gpt"),
    (llm.Phi3Model, "phi3"),
]


def unwrap_for_modelopt_operations(model):
    """Unwraps the model to expose the underlying architecture that Model Optimizer can work with.
    For HuggingFace models, returns the base model. For MCore models, returns the unwrapped version."""

    if isinstance(model, llm.HFAutoModelForCausalLM):
        return model.model
    return unwrap_model(model)


def get_modelopt_decoder_type(model: Union[llm.GPTModel, llm.HFAutoModelForCausalLM]) -> Optional[str]:
    """Infers the modelopt decoder type from GPTModel or HFAutoModelForCausalLM.

    Args:
        model (GPTModel | HFAutoModelForCausalLM): The model to infer the decoder type from.
    Returns:
        Optional[str]: The inferred decoder type or None if no match is found.
    """
    if isinstance(model, llm.HFAutoModelForCausalLM):
        for k, v in huggingface_model_type_pattern_match.items():
            if k.lower() in type(model.model).__name__.lower():
                return v
    else:
        for config_class, decoder_type in gpt_model_type:
            if isinstance(model, config_class):
                return decoder_type

    return None
