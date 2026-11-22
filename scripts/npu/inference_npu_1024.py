# 板上推理适配：1024 patch 版 vision.cix（输入 [1024,1536]，输出 4×[256,2560]）
# 基于官方 inference_npu.py 修改三处：reshape 100→256；图像强制 512×512；引擎单例复用。
# 用法（Orange Pi，venv 为 ~/qwen3vl-npu/venv）：
#   venv/bin/python inference_npu_1024.py --image-path aoi.png --question '...' --vision-path vision_1024.cix
from __future__ import annotations

import argparse
import sys

import numpy as np
import torch
from transformers import AutoProcessor, Qwen3VLForConditionalGeneration

from NOE_Engine import EngineInfer

N_TOKENS = 256          # 1024 patch / 4（merger）= 256 token
IMG_SIZE = 512          # 32×16 = 512px → 32×32 = 1024 patch

_engine = None


def get_engine(vision_path: str):
    global _engine
    if _engine is None:
        _engine = EngineInfer(vision_path)
    return _engine


def make_qwen3_vl_visual_npu_forward(vision_path: str):
    def forward(hidden_states, grid_thw, **kwargs):
        engine = get_engine(vision_path)
        input_data = hidden_states.float().detach().cpu().numpy()
        assert input_data.size == 1024 * 1536, (
            f"输入元素数 {input_data.size} ≠ {1024 * 1536}，请确认图像已被处理为 512×512")
        output_data = engine.forward(input_data)
        npu_output = [np.reshape(o, (N_TOKENS, 2560)) for o in output_data]
        dev, dt = hidden_states.device, hidden_states.dtype
        to_dev = lambda arr: torch.from_numpy(arr).to(device=dev, dtype=dt)
        return to_dev(npu_output[0]), [to_dev(arr) for arr in npu_output[1:]]
    return forward


def main() -> None:
    parser = argparse.ArgumentParser(description="Qwen3-VL-4B NPU 混合推理（1024 patch）")
    parser.add_argument("--model-dir", default="Qwen3-VL-4B-Instruct")
    parser.add_argument("--image-path", required=True)
    parser.add_argument("--question", default="请描述这张图片.")
    parser.add_argument("--vision-path", default="vision_1024.cix")
    parser.add_argument("--max-new-tokens", type=int, default=400)
    args = parser.parse_args()

    model = Qwen3VLForConditionalGeneration.from_pretrained(
        args.model_dir, dtype="auto", device_map="auto",
        local_files_only=True, attn_implementation="eager",
    )
    processor = AutoProcessor.from_pretrained(args.model_dir)

    # 强制 512×512，保证 processor 输出恰好 1024 patch
    from PIL import Image
    img = Image.open(args.image_path).convert("RGB").resize((IMG_SIZE, IMG_SIZE))
    inputs = processor.apply_chat_template(
        [{"role": "user", "content": [
            {"type": "image", "image": img},
            {"type": "text", "text": args.question.strip() or "请描述这张图片."},
        ]}],
        tokenize=True, add_generation_prompt=True,
        return_dict=True, return_tensors="pt",
    ).to(model.device)
    pv = inputs["pixel_values"]
    assert pv.shape[-2] == 1024, f"pixel_values patch 数 {pv.shape} ≠ 1024"

    visual = model.model.visual
    original_forward = visual.forward
    visual.forward = make_qwen3_vl_visual_npu_forward(args.vision_path)
    try:
        generated = model.generate(**inputs, max_new_tokens=args.max_new_tokens)
        print(processor.batch_decode(generated, skip_special_tokens=True)[0], flush=True)
    finally:
        visual.forward = original_forward


if __name__ == "__main__":
    main()
