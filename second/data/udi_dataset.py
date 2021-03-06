# udi dataset process module
# modiflied from nuscenes_dataset.py

import json
import pickle
import time
import random
from copy import deepcopy
from functools import partial
from pathlib import Path
import subprocess

import fire
import numpy as np 
import os

from second.core import box_np_ops
from second.core import preprocess as prep
from second.data import kitti_common as kitti
from second.data.dataset import Dataset, register_dataset
from second.utils.eval import get_coco_eval_result, get_official_eval_result
from second.utils.progress_bar import progress_bar_iter as prog_bar 
from second.utils.timer import simple_timer

@register_dataset
class UDIDataset(Dataset):
    NumPointFeatures = 4
    NameMapping = {
        'car': 'car',
        'pedestrian': 'pedestrian',
        'cyclist': 'cyclist',
        'truck': 'truck',
        'forklift': 'forklift',
        'golf car': 'golf car',
        'motorcyclist': 'motorcyclist',
        'bicycle': 'bicycle',
        'motorbike': 'motorbike'
    }
    DefaultAttribute = {
        "car": "object_action_parked",
        "pedestrain": "object_action_walking",
        "bicycle": "object_action_driving_straight_forward",
        "motorcycle": "object_action_parked",
        "other_vehicle": "object_action_driving_straight_forward",
        "emergency_vehicle": "object_action_driving_straight_forward",
        "truck": "object_action_parked",
        "animal": "",
        "bus": "object_action_driving_straight_forward",
    }

    def __init__(self,
                 root_path,
                 info_path,
                 class_names=None,
                 prep_func=None,
                 num_point_features=None):
        self._root_path = Path(root_path)
        self._info_path = Path(info_path)
        with open(info_path, 'rb') as f:
            data = pickle.load(f)
        self._udi_infos = data["infos"]
        self._metadata = data["metadata"]
        self._class_names = class_names
        self._prep_func = prep_func

        self.version = self._metadata["version"]
        self._with_velocity = False
    
    def __len__(self):
        return len(self._udi_infos)
    
    def __getitem__(self, idx):
        input_dict = self.get_sensor_data(idx)
        example = self._prep_func(input_dict=input_dict)
        example["metadata"] = input_dict["metadata"]
        if "anchors_mask" in example:
            example["anchors_mask"] = example["anchors_mask"].astype(np.uint8)
        return example
    
    def get_sensor_data(self, query):
        idx = query
        if isinstance(query, dict):
            assert "lidar" in query
            idx = query["lidar"]["idx"]
        info = self._udi_infos[idx]
        res = {
            "lidar": {
                "type": "lidar",
                "points": None,
            },
            "metadata": {
                "token": info["token"]
            },
        }
        lidar_path = Path(info['lidar_path'])

        points = np.fromfile(str(lidar_path), dtype=np.float32).reshape((-1,4))

        points[:, 3] /= 255

        res["lidar"]["points"] = points
        
        if 'gt_boxes' in info:
            res["lidar"]["annotations"] = {
                'boxes': info["gt_boxes"],
                'names': info["gt_names"]
            }
        return res

    def evaluation_udi(self, detections, output_dir):
        version = self.version
        eval_set_map = {
            # "v1.0-mini": "mini_train",
            "v1.0-trainval": "val",
        }
        # gt_annos = self.ground_truth_annotations
        # if gt_annos is None:
            # return None
        udi_annos = {}
        mapped_class_names = self._class_names
        token2info = {}
        for info in self._udi_infos:
            token2info[info["token"]] = info
        for det in detections:
            annos = []
            boxes = _second_det_to_udi_box(det)
            for i, box in enumerate(boxes):
                name = mapped_class_names[box.label]
                velocity = box.velocity[:2].tolist()
                box.velocity = np.array([*velocity, 0.0])
            for i, box in enumerate(boxes):
                name = mapped_class_names[box.label]
                velocity = box.velocity[:2].tolist()
                nusc_anno = {
                    "sample_token": det["metadata"]["token"],
                    "translation": box.center.tolist(),
                    "size": box.wlh.tolist(),
                    "rotation": box.orientation.elements.tolist(),
                    "velocity": velocity,
                    "detection_name": name,
                    "detection_score": box.score,
                    "attribute_name": "",
                }
                annos.append(nusc_anno)
            udi_annos[det["metadata"]["token"]] = annos
        nusc_submissions = {
            "meta": {
                "use_camera": False,
                "use_lidar": False,
                "use_radar": False,
                "use_map": False,
                "use_external": False,
            },
            "results": udi_annos,
        }
        res_path = Path(output_dir) / "results_udi.json"
        with open(res_path, "w") as f:
            json.dump(nusc_submissions, f)
        eval_main_file = Path(__file__).resolve().parent / "udi_eval.py"
        # why add \"{}\"? to support path with spaces.
        cmd = f"python3 {str(eval_main_file)} --root_path=\"{str(self._root_path)}\""
        cmd += f" --info_path=\"{str(self._info_path)}\""
        cmd += f" --version={self.version}"
        cmd += f" --res_path=\"{str(res_path)}\" --eval_set={eval_set_map[self.version]}"
        cmd += f" --output_dir=\"{output_dir}\""
        # use subprocess can release all nusc memory after evaluation
        subprocess.check_output(cmd, shell=True)
        with open(Path(output_dir) / "metrics_summary.json", "r") as f:
            metrics = json.load(f)
        detail = {}
        res_path.unlink()  # delete results_nusc.json since it's very large
        result = f"Nusc {version} Evaluation\n"
        for name in mapped_class_names:
            detail[name] = {}
            for k, v in metrics["label_aps"][name].items():
                detail[name][f"dist@{k}"] = v
            tp_errs = []
            tp_names = []
            for k, v in metrics["label_tp_errors"][name].items():
                detail[name][k] = v
                tp_errs.append(f"{v:.4f}")
                tp_names.append(k)
            threshs = ', '.join(list(metrics["label_aps"][name].keys()))
            scores = list(metrics["label_aps"][name].values())
            scores = ', '.join([f"{s * 100:.2f}" for s in scores])
            result += f"{name} Nusc dist AP@{threshs} and TP errors\n"
            result += scores
            result += "\n"
            result += ', '.join(tp_names) + ": " + ', '.join(tp_errs)
            result += "\n"
        return {
            "results": {
                "nusc": result
            },
            "detail": {
                "nusc": detail
            },
        }


    def evaluation(self, detections, output_dir):
        res_udi = self.evaluation_udi(detections, output_dir)
        res = {
            "results": {
                "nusc": res_udi["result"]["nusc"],
            },
            "detail": {
                "eval.nusc": res_udi["detail"]["nusc"],
            },
        }
        return res

def _second_det_to_udi_box(detection):
    from udi_eval import Box
    import pyquaternion
    box3d = detection["box3d_lidar"].detach().cpu().numpy()
    scores = detection["scores"].detach().cpu().numpy()
    labels = detection["label_preds"].detach().cpu().numpy()
    box3d[:, 6] = -box3d[:, 6] - np.pi/2
    box_list = []
    for i in range(box3d.shape[0]):
        quat = pyquaternion.Quaternion(axis=[0, 0, 1], radians=box3d[i,6])
        velocity = (np.nan, np.nan, np.nan)
        # if box3d.shape[1] == 9:
        #     velocity = (*box3d[i, 7:9], 0.0)
        box = Box(
            box3d[i, :3],
            box3d[i, 3:6],
            quat,
            label=labels[i],
            score=scores[i],
            velocity=velocity)
        box_list.append(box)
    return box_list

# def _lidar_nusc_box_to_global(info, boxes, classes, eval_version="ICLR 2019"):
#     import pyquaternion
#     box_list = []
#     for box in boxes:
#         box.rotate(pyquaternion.Quaternion(info['lidar2ego_rotation']))
#         box.translate(np.array(info['lidar2ego_translation']))
#         box.rotate(pyquaternion.Quaternion(info['ego2global_rotation']))
#         box.translate(np.array(info['ego2global_translation']))
#         box_list.append(box)
#     return box_list

# def _get_available_scenes(lyft):
#     available_scenes = []
#     print("total scene num:", len(lyft.scene))
#     for scene in lyft.scene:
#         scene_token = scene["token"]
#         scene_rec = lyft.get('scene', scene_token)
#         sample_rec = lyft.get('sample', scene_rec['first_sample_token'])
#         sd_rec = lyft.get('sample_data', sample_rec['data']["LIDAR_TOP"])
#         has_more_frames = True
#         scene_not_exist = False
#         while has_more_frames:
#             lidar_path, boxes, _ = lyft.get_sample_data(sd_rec['token'])
#             if not Path(lidar_path).exists():
#                 scenes_not_exist = True
#                 break
#             else:
#                 break
#             if not sd_rec['next'] == "":
#                 sd_rec = lyft.get('sample_data', sd_rec['next'])
#             else:
#                 has_more_frames = False
#         if scene_not_exist:
#             continue
#         available_scenes.append(scene)
#     print("exist scene num:", len(available_scenes))
#     return available_scenes

def _fill_train_infos(root_path):
    train_udi_infos = []
    lidar_root_path = root_path+ "/lidar"
    label_root_path = root_path + "/label"
    img_root_path = root_path + "/image"

    filenames = os.listdir(lidar_root_path)

    for filename in prog_bar(filenames):

        index = filename.split(".")[0]
        lidar_path = lidar_root_path + "/" + index + ".bin"
        cam_path = img_root_path + "/" + index + ".jpg"
        label_path = label_root_path + "/" + index + "_bin.json"
        assert Path(lidar_path).exists()
        assert Path(cam_path).exists()
        assert Path(label_path).exists()

        with open(label_path, encoding='utf-8') as f:
            res = f.read()
        result = json.loads(res)

        boxes = result["elem"]

        info = {
            "lidar_path": lidar_path,
            "cam_front_path": cam_path,
            "filename": filename,
            "token": int(index),
        }
        gt_locs_list = []
        gt_dims_list = []
        print("label file path:", label_path)
        for box in boxes:
            box_loc = box["position"]
            box_size = box["size"]
            box_loc_ = np.array([box_loc["x"],box_loc["y"], box_loc["z"]], dtype=np.float)
            box_size_ = np.array([box_size["width"],box_size["depth"],box_size["height"]], dtype=np.float)
            box_loc_ = box_loc_.reshape(-1, 3)
            box_size_ = box_size_.reshape(-1, 3)
            gt_locs_list.append(box_loc_)
            gt_dims_list.append(box_size_)
        
        locs = np.concatenate(gt_locs_list, axis=0)
        dims = np.concatenate(gt_dims_list, axis=0)
        rots = np.array([b["yaw"] for b in boxes]).reshape(-1, 1)
        names = [b["class"] for b in boxes]

        for i in range(len(names)):
            if names[i] in UDIDataset.NameMapping:
                names[i] = UDIDataset.NameMapping[names[i]]
        names = np.array(names)
        # we need to convert rot to SECOND format.
        # change the rot format will break all checkpoint.
        gt_boxes = np.concatenate([locs, dims, -rots - np.pi / 2], axis=1)

        info["gt_boxes"] = gt_boxes
        info["gt_names"] = names        
        train_udi_infos.append(info)

    return train_udi_infos


def create_udi_infos(root_path):
    # root_path = Path(root_path)
    root_path = str(root_path)

    train_udi_infos = _fill_train_infos(root_path)
    metadata = {
        "version": "v0.1-train",
    }
    print(
        f"train sample: {len(train_udi_infos)}"
    )
    data = {
        "infos": train_udi_infos,
        "metadata": metadata,
    }
    with open(root_path + "/infos_udi_train.pkl", 'wb') as f:
        pickle.dump(data, f)

def get_box_mean(info_path, class_name="car"):
    with open(info_path, 'rb') as f:
        lyft_infos = pickle.load(f)["infos"]
    gt_boxes_list = []
    for info in lyft_infos:
        gt_boxes = info["gt_boxes"]
        gt_names = info["gt_names"]
        mask = np.array([s == class_name for s in info["gt_names"]], dtype=np.bool_)
        gt_names = gt_names[mask]
        gt_boxes = gt_boxes[mask]
        gt_boxes_list.append(gt_boxes.reshape(-1, 7))
    gt_boxes_list = np.concatenate(gt_boxes_list, axis=0)
    return {
        "box3d": gt_boxes_list.mean(0).tolist(),
        "detail": gt_boxes_list
    }

def get_all_box_mean(info_path):
    det_names = set()
    for k, v in UDIDataset.NameMapping.items():
        if v not in det_names:
            det_names.add(v)
    det_names = sorted(list(det_names))
    res = {}
    details = {}
    for k in det_names:
        result = get_box_mean(info_path, k)
        details[k] = result["detail"]
        res[k] = result["box3d"]
    print(json.dumps(res, indent=2))
    return details


if __name__ == "__main__":
    fire.Fire()






