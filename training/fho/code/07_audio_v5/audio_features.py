"""Audio features for each window.

mel  : hand-crafted log-mel statistics + onset/flux statistics (numpy only, always computed)
ast  : optional AudioSet-pretrained Audio Spectrogram Transformer embedding (768-d, needs the
       `transformers` package and a one-time model download); skipped cleanly if unavailable.
Both are computed for the clip audio ("full") and the 8 kHz band-limited version ("hfp8k").
"""
import argparse, json, sys, time
from pathlib import Path
import numpy as np
sys.path.insert(0, str(Path(__file__).resolve().parent))
import common as C

SR, NFFT, WINL, HOP, NMELS = 16000, 512, 400, 160, 64
AST_NAME = "MIT/ast-finetuned-audioset-10-10-0.4593"


def mel_fb():
    hz2mel = lambda f: 2595 * np.log10(1 + f / 700.0)
    mel2hz = lambda m: 700 * (10 ** (m / 2595.0) - 1)
    pts = mel2hz(np.linspace(hz2mel(50), hz2mel(SR / 2), NMELS + 2))
    bins = np.floor((NFFT + 1) * pts / SR).astype(int)
    fb = np.zeros((NMELS, NFFT // 2 + 1))
    for m in range(1, NMELS + 1):
        l, c, r = bins[m - 1], bins[m], bins[m + 1]
        for k in range(l, c):
            fb[m - 1, k] = (k - l) / max(1, c - l)
        for k in range(c, r):
            fb[m - 1, k] = (r - k) / max(1, r - c)
    return fb


FB = mel_fb()
HANN = np.hanning(WINL)


def mel_features(wave_i16):
    x = wave_i16.astype(np.float64) / 32768.0
    n = 1 + (len(x) - WINL) // HOP
    idx = np.arange(WINL)[None, :] + HOP * np.arange(n)[:, None]
    fr = x[idx] * HANN
    spec = np.abs(np.fft.rfft(fr, NFFT)) ** 2
    M = np.log(spec @ FB.T + 1e-10)                                   # T x 64
    flux = np.maximum(0, np.diff(M, axis=0)).sum(1)                   # T-1
    rms = np.sqrt((fr ** 2).mean(1) + 1e-12)
    tail = int(0.5 * SR / HOP)                                        # last 0.5 s
    f_mu, f_sd = flux.mean(), flux.std() + 1e-9
    stats = [rms.mean(), rms.max(), np.log(rms.max()), flux.mean(), flux.max(), flux.std(), flux.argmax() / len(flux),
             float((flux > f_mu + 2 * f_sd).sum()), flux[-tail:].max(), flux[:-tail].max()]
    return np.concatenate([M.mean(0), M.max(0), M.std(0), M[-tail:].mean(0) - M[:-tail].mean(0), stats]).astype(np.float32)


def ast_embed(waves, device, log):
    import torch
    from transformers import ASTFeatureExtractor, ASTModel
    fe = ASTFeatureExtractor.from_pretrained(AST_NAME)
    model = ASTModel.from_pretrained(AST_NAME).eval().to(device)
    out = []
    for s in range(0, len(waves), 32):
        batch = [w.astype(np.float32) / 32768.0 for w in waves[s:s + 32]]
        inp = fe(batch, sampling_rate=SR, return_tensors="pt")
        with torch.inference_mode():
            o = model(**{k: v.to(device) for k, v in inp.items()})
        out.append(o.pooler_output.float().cpu().numpy())
        if (s // 32) % 20 == 0:
            log(f"AST {min(s + 32, len(waves))}/{len(waves)}")
    return np.concatenate(out).astype(np.float32)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--audio", type=Path, required=True)
    ap.add_argument("--output", type=Path, required=True)
    ap.add_argument("--device", default="cuda")
    ap.add_argument("--skip-ast", action="store_true")
    a = ap.parse_args()
    t0 = time.monotonic()
    z = np.load(a.audio)
    ids, full, hfp = z["window_ids"], z["full"], z["hfp8k"]
    feats = {"window_ids": ids,
             "mel_full": np.stack([mel_features(w) for w in full]) if len(full) else np.zeros((0, 266), np.float32),
             "mel_hfp8k": np.stack([mel_features(w) for w in hfp]) if len(hfp) else np.zeros((0, 266), np.float32)}
    print(f"MEL_DONE {len(ids)} {time.monotonic() - t0:.0f}s", flush=True)
    info = {"mel_dims": int(feats["mel_full"].shape[1]) if len(ids) else 0, "ast": "skipped"}
    if not a.skip_ast and len(ids):
        try:
            feats["ast_full"] = ast_embed(full, a.device, lambda m: print(m, flush=True))
            feats["ast_hfp8k"] = ast_embed(hfp, a.device, lambda m: print(m, flush=True))
            info["ast"] = {"model": AST_NAME, "dims": int(feats["ast_full"].shape[1])}
            print("AST_DONE", flush=True)
        except Exception as exc:
            info["ast"] = "unavailable: " + f"{type(exc).__name__}: {exc}"[:300]
            print("AST_SKIPPED", info["ast"], flush=True)
    for k, v in feats.items():
        if k != "window_ids" and not np.isfinite(v).all():
            raise ValueError("Non-finite audio features: " + k)
    np.savez(a.output, **feats)
    C.save_json(a.output.with_suffix(".json"), {**info, "windows": int(len(ids)), "seconds": round(time.monotonic() - t0, 1)})
    print("AUDIO_FEATURES_DONE", flush=True)


if __name__ == "__main__":
    main()
