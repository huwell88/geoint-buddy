# 基于官方 scripts/export_onnx.py 修改：grid (1,20,20)→(1,32,32)，输入 320×320→512×512（1024 patch）
# 用法（x86 Linux 主机）：
#   pip install torch transformers==4.57.3
#   python3 export_onnx_1024.py --model-dir Qwen3-VL-4B-Instruct --output-dir model_1024
from __future__ import annotations
import argparse
import os
import sys
import warnings
from pathlib import Path
warnings.filterwarnings("ignore")
os.environ.setdefault("TRANSFORMERS_OFFLINE", "1")
import torch
import torch.nn as nn
import torch.nn.functional as F

# ---- 修改点：固定网格 (1,32,32) = 1024 patch，对应 512x512 输入 ----
QWEN3_VL_ONNX_FIXED_GRID_THW: tuple[int, int, int] = (1, 32, 32)


class Qwen3VlVisualTowerWrapperFixedGrid(nn.Module):
    def __init__(self, vision_module, grid_thw: tuple[int, int, int] | None = None):
        super().__init__()
        self.vm = vision_module
        t, h, w = grid_thw or QWEN3_VL_ONNX_FIXED_GRID_THW
        g = torch.tensor([[t, h, w]], dtype=torch.long)
        self.register_buffer("image_grid_thw", g, persistent=False)

    def _patch_embed_no_conv3d(self, hidden_states: torch.Tensor) -> torch.Tensor:
        proj = self.vm.patch_embed.proj
        weight_2d = proj.weight.view(proj.out_channels, -1)
        return F.linear(hidden_states.to(dtype=proj.weight.dtype), weight_2d, proj.bias)

    def forward(self, pixel_values):
        grid_thw = self.image_grid_thw.to(pixel_values.device)
        hidden_states = self._patch_embed_no_conv3d(pixel_values)
        pos_embeds = self.vm.fast_pos_embed_interpolate(grid_thw)
        hidden_states = hidden_states + pos_embeds
        rotary_pos_emb = self.vm.rot_pos_emb(grid_thw)
        seq_len, _ = hidden_states.size()
        hidden_states = hidden_states.reshape(seq_len, -1)
        rotary_pos_emb = rotary_pos_emb.reshape(seq_len, -1)
        emb = torch.cat((rotary_pos_emb, rotary_pos_emb), dim=-1)
        position_embeddings = (emb.cos(), emb.sin())
        cu_seqlens = torch.repeat_interleave(grid_thw[:, 1] * grid_thw[:, 2], grid_thw[:, 0]).cumsum(
            dim=0,
            dtype=grid_thw.dtype if torch.jit.is_tracing() else torch.int32,
        )
        cu_seqlens = F.pad(cu_seqlens, (1, 0), value=0)
        deepstack_feature_lists: list[torch.Tensor] = []
        for layer_num, blk in enumerate(self.vm.blocks):
            hidden_states = blk(
                hidden_states,
                cu_seqlens=cu_seqlens,
                position_embeddings=position_embeddings,
            )
            if layer_num in self.vm.deepstack_visual_indexes:
                idx = self.vm.deepstack_visual_indexes.index(layer_num)
                deepstack_feature = self.vm.deepstack_merger_list[idx](hidden_states)
                deepstack_feature_lists.append(deepstack_feature)
        hidden_states = self.vm.merger(hidden_states)
        return hidden_states, deepstack_feature_lists


def load_qwen3_vl_model_and_processor(model_dir: str):
    from transformers import AutoProcessor, Qwen3VLForConditionalGeneration
    md = os.path.normpath(model_dir.rstrip("/"))
    model = Qwen3VLForConditionalGeneration.from_pretrained(
        md, dtype="auto", device_map="auto", local_files_only=True,
        attn_implementation="eager",
    )
    processor = AutoProcessor.from_pretrained(md)
    return model, processor


def export_qwen3_vl_vision_onnx(model_dir: str, out_dir: Path) -> list[str]:
    out_dir = out_dir.resolve()
    out_dir.mkdir(parents=True, exist_ok=True)
    device = torch.device("cuda" if torch.cuda.is_available() else "cpu")
    model, _processor = load_qwen3_vl_model_and_processor(model_dir)
    vision = model.model.visual
    wrapper = Qwen3VlVisualTowerWrapperFixedGrid(vision).to(device).eval().float()
    onnx_path = out_dir / "vision.onnx"
    pe = vision.patch_embed
    patch_elems = pe.in_channels * pe.temporal_patch_size * pe.patch_size * pe.patch_size
    t, h, w = QWEN3_VL_ONNX_FIXED_GRID_THW
    n_tokens = t * h * w
    export_pixel = torch.randn(n_tokens, patch_elems, device=device, dtype=torch.float32)
    with torch.no_grad():
        ref_out = wrapper(export_pixel)
    output_names = ["last_hidden_state"] + [f"deepstack_{i}" for i in range(len(ref_out[1]))]
    torch.onnx.export(
        wrapper, (export_pixel,), str(onnx_path),
        input_names=["pixel_values"], output_names=output_names,
        opset_version=17, dynamo=False, do_constant_folding=True,
    )
    print("grid:", QWEN3_VL_ONNX_FIXED_GRID_THW, "tokens:", n_tokens)
    return [str(onnx_path)]


def main() -> None:
    parser = argparse.ArgumentParser(description="导出 Qwen3-VL vision.onnx（1024 patch 版）")
    parser.add_argument("--model-dir", default="Qwen3-VL-4B-Instruct")
    parser.add_argument("--output-dir", default="model_1024")
    args = parser.parse_args()
    try:
        paths = export_qwen3_vl_vision_onnx(args.model_dir, Path(args.output_dir))
    except Exception as e:
        print(e, file=sys.stderr)
        sys.exit(1)
    for p in paths:
        print("OK", p)


if __name__ == "__main__":
    main()
