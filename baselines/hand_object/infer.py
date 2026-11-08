"""Inference-only 100DOH compatibility experiment; no completion labels predicted.

Official network source stays unmodified. The legacy ROI/NMS import is bridged
to installed torchvision ops, preserving the documented coordinate conventions.
This is not a claim of numerical equivalence to the original CUDA binaries.
"""
from pathlib import Path
import argparse
import hashlib
import json
import platform
import sys
import time
import types

BASE = Path(__file__).resolve().parent
sys.path.insert(0, str(BASE / "vendor/hand_object_detector/lib"))
import cv2
import numpy as np
import torch
import torchvision
from torchvision.ops import roi_align, roi_pool, nms as tv_nms
from PIL import Image, ImageDraw, ImageFont

STATE_NAMES = ["no_contact", "self_contact", "other_person_contact", "portable_object_contact", "stationary_object_contact"]
EXPECTED_SHA = "e7f57309da0536d4bb6c4cdf15ab53256790cf4486c6eef59cb8698a9c33bc43"

def sha256(path):
    h = hashlib.sha256()
    with open(path, "rb") as f:
        for b in iter(lambda: f.read(2**20), b""):
            h.update(b)
    return h.hexdigest()

def inclusive_nms(boxes, scores, threshold):
    # Original CPU and CUDA code use (x2-x1+1)*(y2-y1+1).
    # torchvision uses continuous boxes, so expand the upper endpoints for NMS.
    continuous = boxes.clone()
    continuous[:, 2:] += 1
    return tv_nms(continuous, scores, threshold)

class LegacyROIAlign(torch.nn.Module):
    def __init__(self, output_size, spatial_scale, sampling_ratio):
        super().__init__()
        self.output_size, self.spatial_scale = output_size, spatial_scale
        self.sampling_ratio = sampling_ratio
    def forward(self, x, boxes):
        return roi_align(x, boxes, self.output_size, self.spatial_scale,
                         self.sampling_ratio, aligned=False)

class LegacyROIPool(torch.nn.Module):
    def __init__(self, output_size, spatial_scale):
        super().__init__()
        self.output_size, self.spatial_scale = output_size, spatial_scale
    def forward(self, x, boxes):
        return roi_pool(x, boxes, self.output_size, self.spatial_scale)

def install_ops_bridge():
    bridge = types.ModuleType("model.roi_layers")
    bridge.nms, bridge.ROIAlign, bridge.ROIPool = inclusive_nms, LegacyROIAlign, LegacyROIPool
    bridge.roi_align = lambda x, r, o, s, n: roi_align(x, r, o, s, n, aligned=False)
    bridge.roi_pool = roi_pool
    sys.modules["model.roi_layers"] = bridge

def synthetic_checks():
    # Linear ramp permits analytic averages at sampling centers (no boundary clamp).
    x = torch.arange(16, dtype=torch.float32).reshape(1, 1, 4, 4)
    r = torch.tensor([[0., 0., 0., 2., 2.]])
    got = LegacyROIAlign((2, 2), 1., 0)(x, r)
    expected = torch.tensor([[[[2.5, 3.5], [6.5, 7.5]]]])
    torch.testing.assert_close(got, expected, atol=1e-6, rtol=0)
    boxes = torch.tensor([[0.,0.,1.,1.], [1.,0.,2.,1.], [10.,10.,11.,11.]])
    scores = torch.tensor([.9,.8,.7])
    kept = inclusive_nms(boxes, scores, .3)
    assert kept.tolist() == [0, 2]  # inclusive overlap = 1/3, continuous overlap = 0
    # Verify scaled ROI coordinates recover the same analytic grid.
    scaled = r.clone(); scaled[:,1:] *= 16
    torch.testing.assert_close(LegacyROIAlign((2,2), 1/16, 0)(x, scaled), expected)
    return {"passed": True, "roi_ramp_output": got.tolist(), "inclusive_nms_keep": kept.tolist(),
            "roi_align": {"aligned": False, "sampling_ratio": 0, "spatial_scale": 0.0625, "output_size": [7,7]},
            "scope": "Analytic CPU compatibility checks only; original CUDA implementation not executed."}

def draw_overlay(path, row, out):
    im = Image.open(path).convert("RGB")
    draw = ImageDraw.Draw(im)
    font = ImageFont.load_default(size=10)
    for i, obj in enumerate(row["objects"]):
        draw.rectangle(obj["box_xyxy"], outline=(255,195,20), width=2)
        x,y = obj["box_xyxy"][:2]
        draw.text((max(0,x),max(0,y)), f"O{i} {obj['score']:.2f}", fill=(255,195,20),font=font,stroke_width=1,stroke_fill="black")
    for i, hand in enumerate(row["hands"]):
        color = (255,80,90) if hand["side"] == "right" else (70,170,255)
        draw.rectangle(hand["box_xyxy"], outline=color,width=2)
        x,y = hand["box_xyxy"][:2]
        abbrev = ["N","S","O","P","F"][hand["contact_state_id"]]
        draw.text((max(0,x),max(0,y-12)),f"H{i} {hand['side'][0]}:{abbrev} {hand['score']:.2f}",fill=color,font=font,stroke_width=1,stroke_fill="black")
        match = hand["matched_object_index"]
        if match is not None:
            b = hand["box_xyxy"]; ob = row["objects"][match]["box_xyxy"]
            draw.line(((b[0]+b[2])/2,(b[1]+b[3])/2,(ob[0]+ob[2])/2,(ob[1]+ob[3])/2),fill=color,width=1)
    im.save(out)

def predict(model, cfg, path, device):
    from model.rpn.bbox_transform import bbox_transform_inv, clip_boxes
    im = cv2.imread(str(path), cv2.IMREAD_COLOR)
    if im is None:
        raise ValueError(f"Image failed to decode: {path}")
    h,w = im.shape[:2]
    # Exact official demo normalization: BGR float pixels and empirical BGR mean.
    orig = im.astype(np.float32, copy=True)
    orig -= cfg.PIXEL_MEANS
    scale = float(cfg.TEST.SCALES[0]) / min(h,w)
    if np.round(scale * max(h,w)) > cfg.TEST.MAX_SIZE:
        scale = float(cfg.TEST.MAX_SIZE) / max(h,w)
    resized = cv2.resize(orig,None,fx=scale,fy=scale,interpolation=cv2.INTER_LINEAR)
    data = torch.from_numpy(resized.copy()).permute(2,0,1).unsqueeze(0).to(device)
    info = torch.tensor([[resized.shape[0],resized.shape[1],scale]],dtype=torch.float32,device=device)
    zeros = torch.zeros((1,1,5),device=device)
    start = time.perf_counter()
    with torch.inference_mode():
        out = model(data,info,zeros,torch.zeros(1,dtype=torch.long,device=device),zeros)
        rois,scores,deltas = out[:3]; extra = out[-1]
        deltas = (deltas.reshape(-1,4)*torch.tensor(cfg.TRAIN.BBOX_NORMALIZE_STDS,device=device)
                  +torch.tensor(cfg.TRAIN.BBOX_NORMALIZE_MEANS,device=device)).reshape(1,-1,12)
        pred = clip_boxes(bbox_transform_inv(rois[:,:,1:5],deltas,1),info,1)[0]/scale
        scores = scores[0]
        contacts = torch.softmax(extra[0][0][0],dim=-1)
        offsets = extra[1][0][0]
        right_prob = torch.sigmoid(extra[2][0][0]).flatten()
        row = {"input_dimensions": [w,h], "network_dimensions": [resized.shape[1],resized.shape[0]],
               "network_scale": scale, "coordinate_space": "input_png_pixels_xyxy_inclusive_legacy",
               "hands": [], "objects": []}
        for cls, dest in [(1,"objects"),(2,"hands")]:
            inds = torch.nonzero(scores[:,cls] > .5).flatten()
            if len(inds) == 0:
                continue
            b = pred[inds,cls*4:(cls+1)*4]
            s = scores[inds,cls]
            order = torch.argsort(s,descending=True)
            keep = inclusive_nms(b[order],s[order],float(cfg.TEST.NMS))
            for k in order[keep].tolist():
                index = int(inds[k]); box = b[k].cpu().tolist()
                det = {"box_xyxy":box,"score":float(s[k]),"proposal_index":index}
                if cls == 2:
                    state = int(contacts[index].argmax())
                    det.update({"contact_state_id":state,"contact_state":STATE_NAMES[state],
                                "contact_probabilities":contacts[index].cpu().tolist(),
                                "right_probability":float(right_prob[index]),
                                "side":"right" if right_prob[index] > .5 else "left",
                                "offset_vector_raw":offsets[index].cpu().tolist(),
                                "matched_object_index":None})
                row[dest].append(det)
        # Official matching heuristic; no identity/tracking or contact likelihood gate added.
        for hand in row["hands"]:
            if hand["contact_state_id"] == 0 or not row["objects"]:
                continue
            b = hand["box_xyxy"]; mag,dx,dy = hand["offset_vector_raw"]
            point = np.array([(b[0]+b[2])/2+mag*10000*dx,(b[1]+b[3])/2+mag*10000*dy])
            centers = np.array([[(o["box_xyxy"][0]+o["box_xyxy"][2])/2,(o["box_xyxy"][1]+o["box_xyxy"][3])/2] for o in row["objects"]])
            hand["matched_object_index"] = int(np.argmin(np.sum((centers-point)**2,axis=1)))
            hand["matching_method"] = "official_offset_nearest_object_center; no distance rejection"
        if device.type == "cuda": torch.cuda.synchronize()
    row["inference_postprocess_seconds"] = time.perf_counter()-start
    return row

def load_model(checkpoint_path, device):
    from model.utils.config import cfg, cfg_from_file
    from model.faster_rcnn.resnet import resnet
    cfg_from_file(str(BASE / "vendor/hand_object_detector/cfgs/res101.yml"))
    cfg.USE_GPU_NMS = device.type == "cuda"; cfg.CUDA = device.type == "cuda"
    if sha256(checkpoint_path) != EXPECTED_SHA:
        raise ValueError("Checkpoint checksum mismatch")
    checkpoint = torch.load(checkpoint_path, map_location="cpu", weights_only=True)
    if checkpoint["class_agnostic"] is not False or checkpoint["pooling_mode"] != "align":
        raise ValueError("Unsupported checkpoint configuration")
    model = resnet(np.asarray(["__background__", "targetobject", "hand"]), 101,
                   pretrained=False, class_agnostic=False)
    model.create_architecture()
    model.load_state_dict(checkpoint["model"], strict=True)
    model.to(device).eval()
    return model, cfg, len(checkpoint["model"])

def main():
    import tempfile
    parser = argparse.ArgumentParser(description="Offline 100DOH hand/object contact inference. No event prediction.")
    parser.add_argument("--images", nargs="+", type=Path, help="Square RGB images; use prepare_images.py first for other aspect ratios")
    parser.add_argument("--output", type=Path, help="New or empty output directory")
    parser.add_argument("--checkpoint", type=Path, default=BASE / "checkpoints/faster_rcnn_1_8_89999.pth")
    parser.add_argument("--device", choices=["cpu", "cuda"], default="cpu")
    parser.add_argument("--threads", type=int, default=5)
    parser.add_argument("--smoke-test", action="store_true", help="Run analytic operator checks, strict checkpoint load, and one synthetic-image inference")
    args = parser.parse_args()
    if not args.smoke_test and (not args.images or not args.output):
        parser.error("--images and --output are required outside --smoke-test")
    if args.threads < 1:
        parser.error("--threads must be positive")
    if args.output and args.output.exists() and any(args.output.iterdir()):
        raise FileExistsError("Refusing to overwrite a nonempty output directory")
    torch.set_num_threads(args.threads); torch.manual_seed(0); np.random.seed(0)
    install_ops_bridge()
    checks = synthetic_checks()
    device = torch.device(args.device)
    model, cfg, tensor_count = load_model(args.checkpoint, device)
    if args.smoke_test:
        with tempfile.TemporaryDirectory(prefix="hand-model-smoke-") as temporary:
            image = Path(temporary) / "synthetic.png"
            Image.new("RGB", (600, 600), (230, 230, 230)).save(image)
            row = predict(model, cfg, image, device)
        print(json.dumps({"status":"passed", "analytic_ops":checks["passed"],
                          "strict_checkpoint_load":True, "checkpoint_tensors":tensor_count,
                          "checkpoint_sha256":sha256(args.checkpoint),
                          "synthetic_inference":True,"synthetic_hand_count":len(row["hands"]),
                          "synthetic_object_count":len(row["objects"]),"device":str(device)}))
        return
    args.output.mkdir(parents=True, exist_ok=True)
    overlay_dir = args.output / "overlays"; overlay_dir.mkdir()
    with (args.output / "predictions.jsonl").open("w", encoding="utf-8") as stream:
        for index, image in enumerate(args.images):
            with Image.open(image) as im:
                if im.width != im.height:
                    raise ValueError("Non-square input rejected: letterbox with prepare_images.py first")
            row = predict(model, cfg, image, device)
            row.update(item_id=f"image_{index:05d}", input_filename=image.name,
                       input_sha256=sha256(image), model_completion_label=None)
            overlay = overlay_dir / f"image_{index:05d}.png"
            draw_overlay(image, row, overlay)
            row["overlay_relative_path"] = str(overlay.relative_to(args.output))
            stream.write(json.dumps(row)+"\n"); stream.flush()
            print(f"{index+1}/{len(args.images)} hands={len(row['hands'])} objects={len(row['objects'])}", flush=True)
    receipt = {"model":"100DOH handobj_100K (not +ego)","checkpoint_sha256":sha256(args.checkpoint),
               "official_repo_commit":"e6eec712a498ec7844b97893c8d012cea1a71e09",
               "strict_checkpoint_tensors":tensor_count,"analytic_checks":checks,
               "torch":torch.__version__,"torchvision":torchvision.__version__,
               "device":str(device),"input_count":len(args.images),
               "hand_score_threshold":0.5,"object_score_threshold":0.5,"nms_threshold":cfg.TEST.NMS,
               "input_requirement":"Square letterboxed images; coordinates refer to those images.",
               "completion_predictions":False,"tracking":False,"no_training":True}
    (args.output/"run_receipt.json").write_text(json.dumps(receipt,indent=2)+"\n", encoding="utf-8")

if __name__ == "__main__": main()
