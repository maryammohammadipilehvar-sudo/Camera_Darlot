#!/usr/bin/env python3
"""
manage_watchlist.py — add or remove faces from the Faiss identification database.

Usage:
    # Add a face from image
    python manage_watchlist.py add --name "John Doe" --image john.jpg

    # Add all images in a folder (one identity per subfolder name)
    python manage_watchlist.py add-dir --dir faces/

    # List all watchlist entries
    python manage_watchlist.py list

    # Remove an entry by name
    python manage_watchlist.py remove --name "John Doe"
"""
import argparse, os, json, logging
import cv2, numpy as np

logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
log = logging.getLogger("watchlist")

INDEX_PATH = "watchlist.index"
META_PATH  = "watchlist_meta.json"
DIM = 512


def load_scrfd(model_path="models/scrfd_10g.onnx"):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(model_path, opts,
                                providers=["CUDAExecutionProvider","CPUExecutionProvider"])
    return sess


def load_adaface(model_path="models/adaface_ir50.onnx"):
    import onnxruntime as ort
    opts = ort.SessionOptions()
    opts.graph_optimization_level = ort.GraphOptimizationLevel.ORT_ENABLE_ALL
    sess = ort.InferenceSession(model_path, opts,
                                providers=["CUDAExecutionProvider","CPUExecutionProvider"])
    return sess


def detect_faces(sess, img_bgr):
    h, w = img_bgr.shape[:2]
    inp = cv2.resize(img_bgr, (640, 640))
    inp = (inp.astype(np.float32) - 127.5) / 128.0
    inp = inp.transpose(2, 0, 1)[None]
    name = sess.get_inputs()[0].name
    outs = sess.run(None, {name: inp})
    scores, bboxes = outs[0][0], outs[1][0]
    mask = scores > 0.5
    results = []
    for s, b in zip(scores[mask], bboxes[mask]):
        x1,y1,x2,y2 = b
        results.append((int(x1*w/640), int(y1*h/640), int(x2*w/640), int(y2*h/640), float(s)))
    return results


def get_embedding(scrfd_sess, ada_sess, img_bgr):
    faces = detect_faces(scrfd_sess, img_bgr)
    if not faces:
        return None
    # Use largest face
    faces.sort(key=lambda f: (f[2]-f[0])*(f[3]-f[1]), reverse=True)
    x1,y1,x2,y2,_ = faces[0]
    crop = img_bgr[max(0,y1):y2, max(0,x1):x2]
    if crop.size == 0:
        return None
    face = cv2.resize(crop, (112, 112))
    face = cv2.cvtColor(face, cv2.COLOR_BGR2RGB).astype(np.float32)
    face = (face - 127.5) / 128.0
    face = face.transpose(2, 0, 1)[None]
    name = ada_sess.get_inputs()[0].name
    emb = ada_sess.run(None, {name: face})[0][0]
    emb = emb / (np.linalg.norm(emb) + 1e-6)
    return emb.astype(np.float32)


def load_or_create_index():
    import faiss
    if os.path.exists(INDEX_PATH):
        index = faiss.read_index(INDEX_PATH)
        with open(META_PATH) as f:
            meta = json.load(f)
    else:
        index = faiss.IndexFlatIP(DIM)
        meta = []
    return index, meta


def save_index(index, meta):
    import faiss
    faiss.write_index(index, INDEX_PATH)
    with open(META_PATH, "w") as f:
        json.dump(meta, f, indent=2)


def cmd_add(args):
    import faiss
    scrfd = load_scrfd(); ada = load_adaface()
    img = cv2.imread(args.image)
    if img is None:
        log.error(f"Cannot read image: {args.image}"); return
    emb = get_embedding(scrfd, ada, img)
    if emb is None:
        log.error("No face detected in image."); return
    index, meta = load_or_create_index()
    index.add(emb[None])
    meta.append({"name": args.name, "image": args.image})
    save_index(index, meta)
    log.info(f"✓ Added '{args.name}' (total: {index.ntotal})")


def cmd_add_dir(args):
    import faiss
    scrfd = load_scrfd(); ada = load_adaface()
    index, meta = load_or_create_index()
    added = 0
    for name in os.listdir(args.dir):
        subdir = os.path.join(args.dir, name)
        if not os.path.isdir(subdir): continue
        for fname in os.listdir(subdir):
            if not fname.lower().endswith((".jpg",".jpeg",".png")): continue
            img = cv2.imread(os.path.join(subdir, fname))
            if img is None: continue
            emb = get_embedding(scrfd, ada, img)
            if emb is None:
                log.warning(f"No face: {fname}"); continue
            index.add(emb[None])
            meta.append({"name": name, "image": fname})
            added += 1
            log.info(f"  Added {name} from {fname}")
    save_index(index, meta)
    log.info(f"✓ Added {added} faces (total: {index.ntotal})")


def cmd_list(args):
    if not os.path.exists(META_PATH):
        log.info("Watchlist is empty."); return
    with open(META_PATH) as f:
        meta = json.load(f)
    log.info(f"Watchlist ({len(meta)} entries):")
    for i, m in enumerate(meta):
        log.info(f"  [{i:4d}] {m.get('name')}  — {m.get('image','')}")


def cmd_remove(args):
    import faiss
    if not os.path.exists(INDEX_PATH):
        log.error("No watchlist found."); return
    index, meta = load_or_create_index()
    keep = [i for i,m in enumerate(meta) if m.get("name") != args.name]
    removed = len(meta) - len(keep)
    if removed == 0:
        log.warning(f"Name '{args.name}' not found."); return

    # Rebuild index without removed entries
    embs = np.vstack([index.reconstruct(i) for i in keep]) if keep else np.empty((0, DIM), dtype=np.float32)
    new_index = faiss.IndexFlatIP(DIM)
    if len(embs):
        new_index.add(embs)
    new_meta = [meta[i] for i in keep]
    save_index(new_index, new_meta)
    log.info(f"✓ Removed {removed} entry/entries for '{args.name}' (remaining: {new_index.ntotal})")


def main():
    p = argparse.ArgumentParser()
    sub = p.add_subparsers(dest="cmd")

    a = sub.add_parser("add");     a.add_argument("--name", required=True); a.add_argument("--image", required=True)
    b = sub.add_parser("add-dir"); b.add_argument("--dir", required=True)
    c = sub.add_parser("list")
    d = sub.add_parser("remove");  d.add_argument("--name", required=True)

    args = p.parse_args()
    dispatch = {"add": cmd_add, "add-dir": cmd_add_dir, "list": cmd_list, "remove": cmd_remove}
    if args.cmd in dispatch:
        dispatch[args.cmd](args)
    else:
        p.print_help()

if __name__ == "__main__":
    main()
