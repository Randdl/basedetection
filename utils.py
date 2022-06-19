import torch
from detectron2.data.transforms import ResizeTransform
from detectron2.structures import BoxMode, Instances, Boxes

import detectron2
from detectron2.utils.logger import setup_logger

# import some common libraries
import numpy as np
import os, json, cv2, random

# import some common detectron2 utilities
from detectron2 import model_zoo
from detectron2.config import get_cfg
from detectron2.utils.visualizer import Visualizer

import copy
import detectron2.data.transforms as T
import detectron2.utils.comm as comm
from detectron2.checkpoint import DetectionCheckpointer
from detectron2.data import DatasetMapper, MetadataCatalog, build_detection_train_loader, DatasetCatalog
from detectron2.engine import DefaultPredictor, DefaultTrainer, default_argument_parser, default_setup, launch
from detectron2.evaluation import CityscapesSemSegEvaluator, DatasetEvaluators, SemSegEvaluator
from detectron2.projects.deeplab import add_deeplab_config, build_lr_scheduler
from detectron2.data import detection_utils as utils


def bases_to_delta(anchors, gt_bases):
    x1 = anchors[:, 0]
    y1 = anchors[:, 1]
    x2 = anchors[:, 2]
    y2 = anchors[:, 3]
    proposal_midx = (x1 + x2) / 2
    proposal_midy = (y1 + y2) / 2
    dx = x2 - x1
    dy = y2 - y1
    gt_bases_midx = (gt_bases[:, 0] + gt_bases[:, 2] + gt_bases[:, 4] + gt_bases[:, 6]) / 4
    gt_bases_midy = (gt_bases[:, 1] + gt_bases[:, 3] + gt_bases[:, 5] + gt_bases[:, 7]) / 4
    gt_bases_midx1 = gt_bases[:, 0]
    gt_bases_midy1 = gt_bases[:, 1]
    gt_bases_midx2 = gt_bases[:, 2]
    gt_bases_midy2 = gt_bases[:, 3]

    gt_bases_midx1 = gt_bases_midx1 - gt_bases_midx
    gt_bases_midy1 = gt_bases_midy1 - gt_bases_midy
    gt_bases_midx2 = gt_bases_midx2 - gt_bases_midx
    gt_bases_midy2 = gt_bases_midy2 - gt_bases_midy

    gt_bases_midx = gt_bases_midx - proposal_midx
    gt_bases_midy = gt_bases_midy - proposal_midy
    gt_bases_midx = gt_bases_midx / dx
    gt_bases_midy = gt_bases_midy / dy
    gt_bases_mid = torch.stack((gt_bases_midx, gt_bases_midy), dim=1)

    gt_bases_midx1 = gt_bases_midx1 / dx
    gt_bases_midy1 = gt_bases_midy1 / dy
    gt_bases_midx2 = gt_bases_midx2 / dx
    gt_bases_midy2 = gt_bases_midy2 / dy

    gt_bases_delta = torch.stack((gt_bases_midx, gt_bases_midy, gt_bases_midx1, gt_bases_midy1,
                                  gt_bases_midx2, gt_bases_midy2), dim=1)
    return gt_bases_delta


def transform_instance_annotations(
        annotation, transforms, image_size, *, keypoint_hflip_indices=None
):
    """
    Apply transforms to box, segmentation and keypoints annotations of a single instance.

    It will use `transforms.apply_box` for the box, and
    `transforms.apply_coords` for segmentation polygons & keypoints.
    If you need anything more specially designed for each data structure,
    you'll need to implement your own version of this function or the transforms.

    Args:
        annotation (dict): dict of instance annotations for a single instance.
            It will be modified in-place.
        transforms (TransformList or list[Transform]):
        image_size (tuple): the height, width of the transformed image
        keypoint_hflip_indices (ndarray[int]): see `create_keypoint_hflip_indices`.

    Returns:
        dict:
            the same input dict with fields "bbox", "segmentation", "keypoints"
            transformed according to `transforms`.
            The "bbox_mode" field will be set to XYXY_ABS.
    """
    if isinstance(transforms, (tuple, list)):
        transforms = T.TransformList(transforms)
    # bbox is 1d (per-instance bounding box)
    bbox = annotation["bbox"]
    base = np.transpose(annotation["base"])
    # base = transforms.apply_coords(base)
    # clip transformed bbox to image size
    transforms = ResizeTransform(370, 1224, 370, 1224)
    bbox = transforms.apply_box(np.array([bbox]))[0].clip(min=0)
    # print(bbox)
    base = base.flatten()
    # print(base)
    annotation["bbox"] = np.minimum(bbox, list(image_size + image_size)[::-1])
    annotation["base"] = base
    annotation["bbox_mode"] = BoxMode.XYXY_ABS

    return annotation


def annotations_to_instances(annos, image_size, mask_format="polygon"):
    """
    Create an :class:`Instances` object used by the models,
    from instance annotations in the dataset dict.

    Args:
        annos (list[dict]): a list of instance annotations in one image, each
            element for one instance.
        image_size (tuple): height, width

    Returns:
        Instances:
            It will contain fields "gt_boxes", "gt_classes",
            "gt_masks", "gt_keypoints", if they can be obtained from `annos`.
            This is the format that builtin models expect.
    """
    boxes = [BoxMode.convert(obj["bbox"], obj["bbox_mode"], BoxMode.XYXY_ABS) for obj in annos]
    bases = [obj["base"] for obj in annos]
    target = Instances(image_size)
    target.gt_boxes = Boxes(boxes)
    device = bases.device if isinstance(bases, torch.Tensor) else torch.device("cpu")
    bases = torch.as_tensor(bases, dtype=torch.float32, device=device)
    # print("bases: {}".format(bases.shape))
    if bases.numel() == 0:
        # Use reshape, so we don't end up creating a new tensor that does not depend on
        # the inputs (and consequently confuses jit)
        bases = bases.reshape((0, 4)).to(dtype=torch.float32, device=device)
    target.gt_bases = bases

    classes = [int(obj["category_id"]) for obj in annos]
    classes = torch.tensor(classes, dtype=torch.int64)
    target.gt_classes = classes

    h = [obj["h"] for obj in annos]
    h = torch.tensor(h, dtype=torch.float32)
    target.gt_h = h

    return target


def mapper(dataset_dict):
    dataset_dict = copy.deepcopy(dataset_dict)  # it will be modified by code below
    # can use other ways to read image
    image = utils.read_image(dataset_dict["file_name"], format="BGR")
    # See "Data Augmentation" tutorial for details usage
    auginput = T.AugInput(image)
    transform = T.Resize((370, 1224))(auginput)
    image = torch.tensor(auginput.image.transpose(2, 0, 1))
    annos = [
        transform_instance_annotations(annotation, [transform], image.shape[1:])
        for annotation in dataset_dict.pop("annotations")
    ]
    return {
        # create the format that the model expects
        "image_id": dataset_dict["image_id"],
        "width": dataset_dict["width"],
        "height": dataset_dict["height"],
        "image": image,
        "instances": annotations_to_instances(annos, image.shape[1:])
    }


class Trainer(DefaultTrainer):
    """
    We use the "DefaultTrainer" which contains a number pre-defined logic for
    standard training workflow. They may not work for you, especially if you
    are working on a new research project. In that case you can use the cleaner
    "SimpleTrainer", or write your own training loop.
    """

    @classmethod
    def build_train_loader(cls, cfg):
        # mapper = KittiDatasetMapper(cfg, is_train=True)
        return build_detection_train_loader(cfg, mapper=mapper)
