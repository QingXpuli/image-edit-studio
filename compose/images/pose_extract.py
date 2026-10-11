"""Local pose estimation package; pose_estimated only, not pose_controlled.

引擎:controlnet_aux.OpenposeDetector(本机已验证:权重经 hf-mirror 下载,推理成功)。
说明:DWPose 需要 mmdet/mmcv,Python 3.14 无 mmcv 轮子,安装受阻,故用 OpenPose。
产物:pose.json(归一化关键点)/ pose-overlay.png(骨架图)/ pose-card.md / package.json
全程本地推理;权重已缓存于 ~/.cache/huggingface/hub。
"""
from __future__ import annotations
import argparse, json, os, sys
from pathlib import Path

os.environ.setdefault('HF_HUB_DISABLE_XET', '1')

import numpy as np
from PIL import Image

BODY_PARTS = ['鼻', '颈', '右肩', '右肘', '右腕', '左肩', '左肘', '左腕',
              '右髋', '右膝', '右踝', '左髋', '左膝', '左踝',
              '右眼', '左眼', '右耳', '左耳']


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('image')
    ap.add_argument('--out-dir', required=True)
    a = ap.parse_args()
    src, out = Path(a.image), Path(a.out_dir)
    out.mkdir(parents=True, exist_ok=True)

    try:
        from controlnet_aux import OpenposeDetector
        det = OpenposeDetector.from_pretrained('lllyasviel/Annotators')
    except Exception as e:
        print(json.dumps({'ok': False, 'pose_estimated': False, 'pose_controlled': False,
                          'message': '本机 OpenPose 权重不可用;未生成姿态结果。',
                          'detail': str(e)[-500:]}, ensure_ascii=False))
        return 2

    im = Image.open(src).convert('RGB')
    try:
        poses = det.detect_poses(np.asarray(im), include_hand=True, include_face=False)
    except Exception as e:
        print(json.dumps({'ok': False, 'pose_estimated': False, 'pose_controlled': False,
                          'message': 'OpenPose 推理失败;未生成姿态结果。',
                          'detail': str(e)[-500:]}, ensure_ascii=False))
        return 2

    people = []
    for pose in poses:
        body = pose.body
        people.append({
            'keypoints': [None if p is None else {'x': round(float(p.x), 6), 'y': round(float(p.y), 6)}
                          for p in body.keypoints],
            'total_score': round(float(body.total_score), 3),
            'total_parts': int(body.total_parts),
        })
    pose_json = {'source': str(src), 'pose_estimated': True, 'pose_controlled': False,
                 'keypoint_names': BODY_PARTS, 'people': people}
    (out / 'pose.json').write_text(json.dumps(pose_json, ensure_ascii=False, indent=1), encoding='utf-8')

    overlay = det(im, include_body=True, include_hand=True, include_face=False, output_type='pil')
    if not isinstance(overlay, Image.Image):
        overlay = Image.fromarray(np.asarray(overlay))
    overlay_path = out / 'pose-overlay.png'
    overlay.convert('RGB').save(overlay_path)

    lines = ['POSE (OpenPose 本地关键点估计;pose_estimated=true, pose_controlled=false)',
             f'- 检测到人体: {len(people)}']
    if people:
        visible = [(BODY_PARTS[j], p['x'], p['y']) for j, p in enumerate(people[0]['keypoints']) if p]
        lines.append('- 可见关键点: ' + ', '.join(f'{n}({x:.2f},{y:.2f})' for n, x, y in visible))
        missing = [BODY_PARTS[j] for j, p in enumerate(people[0]['keypoints']) if p is None and j < 14]
        if missing:
            lines.append('- 未检出(多因参考图裁切): ' + ', '.join(missing))
    lines += ['- 关键点完整坐标见 pose.json(归一化 x/y)。',
              '- 头部/肩线/躯干/肘腕/髋膝/重心结合 overlay 人工复核。',
              '',
              'DO NOT INCLUDE',
              '- 姿态源人物的脸、发型、服装、配饰、道具',
              '- 参考图身份泄漏',
              '',
              '验收',
              '- 成图身份来自图一;姿态来自本 pose 包;',
              '- 当前通道只证明 pose_estimated,不证明骨骼控制。']
    (out / 'pose-card.md').write_text('\n'.join(lines), encoding='utf-8')

    package = {'ok': True, 'source': str(src), 'out_dir': str(out),
               'pose_json': str(out / 'pose.json'), 'overlay': str(overlay_path),
               'card': str(out / 'pose-card.md'),
               'pose_estimated': True, 'pose_controlled': False,
               'people': len(people)}
    (out / 'package.json').write_text(json.dumps(package, ensure_ascii=False, indent=1), encoding='utf-8')
    print(json.dumps(package, ensure_ascii=False))
    return 0


if __name__ == '__main__':
    sys.exit(main())
