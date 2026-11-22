# 生成 1024 patch 版校准数据：[N,1024,1536] float32 → datasets/calibration_data_1024.npy
# 用真实遥感拼图经 Qwen3-VL processor 预处理采样，分布贴近推理实际输入。
# 用法（x86 主机，与导出同环境）：
#   python3 gen_calibration.py --model-dir Qwen3-VL-4B-Instruct --images img1.png img2.png ... --out datasets/calibration_data_1024.npy
# 无 --images 时退化为随机噪声（FP16 量化下可用，但优先真实图）。
import argparse
import numpy as np
import torch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--model-dir", default="Qwen3-VL-4B-Instruct")
    ap.add_argument("--images", nargs="*", default=[])
    ap.add_argument("--n-random", type=int, default=8, help="随机样本数（无图或补量用）")
    ap.add_argument("--out", default="datasets/calibration_data_1024.npy")
    args = ap.parse_args()

    samples = []
    if args.images:
        from transformers import AutoProcessor
        from PIL import Image
        processor = AutoProcessor.from_pretrained(args.model_dir, local_files_only=True)
        for p in args.images:
            img = Image.open(p).convert("RGB").resize((512, 512))
            out = processor(images=img, return_tensors="pt")
            pv = out["pixel_values"].float()          # [1024,1536]（512/16=32 → 32×32）
            assert pv.shape == (1024, 1536), f"{p}: 得到 {tuple(pv.shape)}，应为 (1024,1536)"
            samples.append(pv.numpy())
            print("sample from", p)
    n_rand = max(0, args.n_random + (0 if samples else 0) - len(samples)) if samples else args.n_random
    for _ in range(n_rand):
        samples.append(np.random.randn(1024, 1536).astype(np.float32))
    arr = np.stack(samples)                            # [N,1024,1536]
    import os
    os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
    np.save(args.out, arr)
    print("saved", args.out, arr.shape, arr.dtype)


if __name__ == "__main__":
    main()
