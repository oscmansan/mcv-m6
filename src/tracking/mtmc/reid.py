import os
import pickle
import pprint
from collections import defaultdict

import cv2
import numpy as np
from sklearn.cluster import DBSCAN
from sklearn.metrics.pairwise import pairwise_distances
from tqdm import tqdm
import networkx as nx

from evaluation.idf1 import MOTAcumulator
from tracking.mtmc.camera import read_calibration, read_timestamps, angle_to_cam, bbox2gps, time_range, angle
from tracking.mtmc.encoder import Encoder
from tracking.track import Track
from utils.aicity_reader import parse_annotations_from_txt, group_by_frame, group_by_id, group_in_tracks


def is_static(track, thresh=175):
    std = np.std([det.center for det in track], axis=0)
    return np.all(std < thresh)


def get_track_embedding(track, cap, encoder, max_views=32):
    batch = []
    for det in np.random.permutation(track)[:max_views]:
        cap.set(cv2.CAP_PROP_POS_FRAMES, det.frame)
        ret, img = cap.read()
        img = img[int(det.ytl):int(det.ybr), int(det.xtl):int(det.xbr)]
        if img.size > 0:
            batch.append(img)

    embeddings = encoder.get_embeddings(batch)

    # combine track embeddings by averaging them
    embedding = embeddings.mean(axis=0)

    return embedding


def get_track_embeddings(tracks_by_cam, cap, encoder, batch_size=512, save_path=None):
    embeddings = defaultdict(dict)
    for cam in tqdm(tracks_by_cam, desc='Computing embeddings', leave=True):
        # process camera detections frame by frame
        if isinstance(tracks_by_cam[cam][next(iter(tracks_by_cam[cam]))], Track):
            detections = [det for track in tracks_by_cam[cam].values() for det in track.get_track()]
        else:
            detections = [det for track in tracks_by_cam[cam].values() for det in track]
        detections_by_frame = group_by_frame(detections)

        cap[cam].set(cv2.CAP_PROP_POS_FRAMES, 0)
        length = int(cap[cam].get(cv2.CAP_PROP_FRAME_COUNT))

        track_embeddings = defaultdict(list)
        batch = []
        ids = []
        for _ in tqdm(range(length), desc=f'cam={cam}', leave=False):
            # read frame
            frame = int(cap[cam].get(cv2.CAP_PROP_POS_FRAMES))
            _, img = cap[cam].read()
            if frame not in detections_by_frame:
                continue

            # crop and accumulate frame detections
            for det in detections_by_frame[frame]:
                crop = img[int(det.ytl):int(det.ybr), int(det.xtl):int(det.xbr)]
                if crop.size > 0:
                    batch.append(crop)
                    ids.append(det.id)

            # compute embeddings if enough detections in batch
            if len(batch) >= batch_size:
                embds = encoder.get_embeddings(batch)
                for id, embd in zip(ids, embds):
                    track_embeddings[id].append(embd)
                batch.clear()
                ids.clear()

        # compute embeddings of last batch
        if len(batch) > 0:
            embds = encoder.get_embeddings(batch)
            for id, embd in zip(ids, embds):
                track_embeddings[id].append(embd)

        # combine track embeddings by averaging them
        for id, embds in track_embeddings.items():
            embeddings[cam][id] = np.stack(embds).mean(axis=0)

    # save embeddings
    if save_path is not None:
        os.makedirs(os.path.dirname(save_path), exist_ok=True)
        with open(save_path, 'wb') as f:
            pickle.dump(embeddings, f)

    return embeddings


def reid_exhaustive(root, seq, model_path):
    seq_path = os.path.join(root, 'train', seq)
    cams = sorted([f for f in os.listdir(seq_path) if not f.startswith('.')])

    # read data
    tracks_by_cam = {
        cam: group_by_id(parse_annotations_from_txt(os.path.join(seq_path, cam, 'mtsc', 'mtsc_tc_mask_rcnn.txt'))) for
        cam in cams}
    cap = {cam: cv2.VideoCapture(os.path.join(root, cam, 'vdo.avi')) for cam in cams}

    # filter out static tracks
    for cam in cams:
        tracks_by_cam[cam] = dict(filter(lambda x: not is_static(x[1]), tracks_by_cam[cam].items()))

    # initialize encoder
    encoder = Encoder(path=model_path)
    encoder.eval()

    # compute all embeddings
    model_name = os.path.split(model_path)[-1].split('.')[0]
    embeddings_file = f'src/tracking/metric_learning/embeddings/{seq}_{model_name}.pkl'
    if os.path.exists(embeddings_file):
        with open(embeddings_file, 'rb') as f:
            embeddings = pickle.load(f)
    else:
        embeddings = get_track_embeddings(tracks_by_cam, cap, encoder, save_path=embeddings_file)

    # cluster embeddings to associate tracks
    clustering = DBSCAN(eps=3, min_samples=2)
    clustering.fit(np.stack(list(embeddings.values())))
    clusters = defaultdict(list)
    for id, label in zip(embeddings.keys(), clustering.labels_):
        clusters[label].append(id)
    pprint.pprint(clusters)

    # TODO complete this code

    return tracks_by_cam


def reid_spatiotemporal(root, seq, model_path, metric='euclidean', thresh=10):
    seq_path = os.path.join(root, 'train', seq)
    cams = sorted([f for f in os.listdir(seq_path) if not f.startswith('.')])

    # read data
    tracks_by_cam = {
        cam: group_in_tracks(parse_annotations_from_txt(os.path.join(seq_path, cam, 'mtsc', 'mtsc_tc_mask_rcnn.txt')),
                             cam) for cam in cams}
    cap = {cam: cv2.VideoCapture(os.path.join(seq_path, cam, 'vdo.avi')) for cam in cams}
    fps = {cam: cap[cam].get(cv2.CAP_PROP_FPS) for cam in cams}
    H = {cam: read_calibration(os.path.join(seq_path, cam, 'calibration.txt')) for cam in cams}
    timestamp = read_timestamps(os.path.join(root, 'cam_timestamp', f'{seq}.txt'))

    # filter out static tracks
    for cam in cams:
        tracks_by_cam[cam] = dict(filter(lambda x: not is_static(x[1].get_track()), tracks_by_cam[cam].items()))

    # initialize encoder
    encoder = Encoder(path=model_path)
    encoder.eval()

    # compute all embeddings
    model_name = os.path.split(model_path)[-1].split('.')[0]
    embeddings_file = f'src/tracking/metric_learning/embeddings/{seq}_{model_name}.pkl'
    if os.path.exists(embeddings_file):
        with open(embeddings_file, 'rb') as f:
            embeddings = pickle.load(f)
    else:
        embeddings = get_track_embeddings(tracks_by_cam, cap, encoder, save_path=embeddings_file)

    for cam1 in cams:
        for id1, track1 in tracks_by_cam[cam1].items():
            dets1 = track1.get_track()
            dets1.sort(key=lambda det: det.frame)
            dir1 = bbox2gps(dets1[-1].bbox, H[cam1]) - bbox2gps(dets1[-min(int(fps[cam1]), len(dets1) - 1)].bbox,
                                                                H[cam1])
            range1 = time_range(dets1, timestamp[cam1], fps[cam1])

            candidates = []
            for cam2 in cams:
                if cam2 == cam1:
                    continue

                if angle_to_cam(dets1, H[cam1], cam2) < 45:  # going towards the camera
                    for id2, track2 in tracks_by_cam[cam2].items():
                        dets2 = track2.get_track()
                        dets2.sort(key=lambda det: det.frame)
                        dir2 = bbox2gps(dets2[min(int(fps[cam2]), len(dets2) - 1)].bbox, H[cam2]) - bbox2gps(
                            dets2[0].bbox, H[cam2])
                        range2 = time_range(dets2, timestamp[cam2], fps[cam2])

                        if range2[0] >= range1[0]:  # car is detected later in second camera
                            if angle(dir1, dir2) < 15:  # tracks have similar direction
                                if not track2.get_prev_track() and not track1.get_next_track():
                                    # track has not been previously matched to another track from the same direction
                                    candidates.append((cam2, id2))

            if len(candidates) > 0:
                dist = pairwise_distances([embeddings[cam1][id1]],
                                          [embeddings[cam2][id2] for cam2, id2 in candidates],
                                          metric).flatten()
                ind = dist.argmin()
                if dist[ind] < thresh:
                    # merge matched tracks
                    cam2, id2 = candidates[ind]
                    tracks_by_cam[cam1][id1].set_next_track((cam2, id2))
                    tracks_by_cam[cam2][id2].set_prev_track((cam1, id1))

    starting_tracks = []
    for cam, tracks in tracks_by_cam.items():
        for id, track in tracks.items():
            if track.get_next_track() and not track.get_prev_track():
                starting_tracks.append(track)

    # propagate ids through tracks connected to starting tracks
    results = defaultdict(dict)
    track_count = 1
    for track in starting_tracks:
        track.id = track_count
        results[track.camera][track_count] = track
        next_track = track.get_next_track()

        while next_track:
            cam_next, id_next = next_track
            track_to_propagate = tracks_by_cam[cam_next][id_next]
            track_to_propagate.id = track_count
            results[track_to_propagate.camera][track_count] = track_to_propagate
            next_track = track_to_propagate.get_next_track()
        track_count += 1

    return results


def reid_spatiotemporal_graph(root, seq, model_path, metric='euclidean'):
    seq_path = os.path.join(root, 'train', seq)
    cams = sorted([f for f in os.listdir(seq_path) if not f.startswith('.')])

    # read data
    tracks_by_cam = {
        cam: group_by_id(parse_annotations_from_txt(os.path.join(seq_path, cam, 'mtsc', 'mtsc_tc_mask_rcnn.txt'))) for
        cam in cams}
    cap = {cam: cv2.VideoCapture(os.path.join(seq_path, cam, 'vdo.avi')) for cam in cams}
    fps = {cam: cap[cam].get(cv2.CAP_PROP_FPS) for cam in cams}
    H = {cam: read_calibration(os.path.join(seq_path, cam, 'calibration.txt')) for cam in cams}
    timestamp = read_timestamps(os.path.join(root, 'cam_timestamp', f'{seq}.txt'))

    # filter out static tracks
    for cam in cams:
        tracks_by_cam[cam] = dict(filter(lambda x: not is_static(x[1]), tracks_by_cam[cam].items()))

    # initialize encoder
    encoder = Encoder(path=model_path)
    encoder.eval()

    # compute all embeddings
    model_name = os.path.split(model_path)[-1].split('.')[0]
    embeddings_file = f'src/tracking/metric_learning/embeddings/{seq}_{model_name}.pkl'
    if os.path.exists(embeddings_file):
        with open(embeddings_file, 'rb') as f:
            embeddings = pickle.load(f)
    else:
        embeddings = get_track_embeddings(tracks_by_cam, cap, encoder, save_path=embeddings_file)

    G = nx.Graph()
    for cam1 in cams:
        for id1, track1 in tracks_by_cam[cam1].items():
            track1.sort(key=lambda det: det.frame)
            dir1 = bbox2gps(track1[-1].bbox, H[cam1]) - bbox2gps(track1[-min(int(fps[cam1]), len(track1) - 1)].bbox,
                                                                 H[cam1])
            range1 = time_range(track1, timestamp[cam1], fps[cam1])

            candidates = []
            for cam2 in cams:
                if cam2 == cam1:
                    continue

                if angle_to_cam(track1, H[cam1], cam2) < 45:  # going towards the camera
                    for id2, track2 in tracks_by_cam[cam2].items():
                        track2.sort(key=lambda det: det.frame)
                        dir2 = bbox2gps(track2[min(int(fps[cam2]), len(track2) - 1)].bbox, H[cam2]) - bbox2gps(
                            track2[0].bbox, H[cam2])
                        range2 = time_range(track2, timestamp[cam2], fps[cam2])

                        if range2[0] >= range1[0]:  # car is detected later in second camera
                            if angle(dir1, dir2) < 15:  # tracks have similar direction
                                candidates.append((cam2, id2))

            if len(candidates) > 0:
                clustering = DBSCAN(eps=0.3, min_samples=2, metric=metric)
                clustering.fit(np.stack([embeddings[cam][id] for cam, id in [(cam1, id1)] + candidates]))
                label1 = clustering.labels_[0]
                if label1 != -1:
                    for (cam2, id2), label2 in zip(candidates, clustering.labels_[1:]):
                        if label2 == label1:
                            G.add_edge((cam1, id1), (cam2, id2))

    groups = []
    while G.number_of_nodes() > 0:
        cliques = nx.find_cliques(G)
        maximal = max(cliques, key=len)
        groups.append(maximal)
        G.remove_nodes_from(maximal)

    results = defaultdict(list)
    for global_id, group in enumerate(groups):
        for cam, id in group:
            track = tracks_by_cam[cam][id]
            for det in track:
                det.id = global_id
            results[cam].append(track)

    return results


def write_results(tracks_by_cam, path):
    for cam, tracks in tracks_by_cam.items():
        lines = []
        for id, track in tracks.items():
            if isinstance(track, Track):
                track = track.get_track()
            for det in track:
                lines.append((det.frame, det.id, int(det.xtl), int(det.ytl), int(det.width), int(det.height),
                              det.score, '-1', '-1', '-1'))
        lines = sorted(lines, key=lambda x: x[0])

        filename = os.path.join(path, cam, 'results.txt')
        os.makedirs(os.path.dirname(filename), exist_ok=True)
        with open(filename, 'w') as file:
            for line in lines:
                file.write(','.join(list(map(str, line))) + '\n')


def reid(root, seq, model_path, reid_method):
    if reid_method == 'exhaustive':
        return reid_exhaustive(root, seq, model_path)
    elif reid_method == 'spatio_temporal_consec':
        return reid_spatiotemporal(root, seq, model_path)
    elif reid_method == 'spatio_temporal_graph':
        return reid_spatiotemporal_graph(root, seq, model_path)


if __name__ == '__main__':
    root = '../../../data/AIC20_track3'
    seq = 'S03'

    results = reid_spatiotemporal_graph(root, seq, model_path='../metric_learning/checkpoints/epoch_19__ckpt.pth')
    write_results(results, path=os.path.join('results', seq))

    accumulator = MOTAcumulator()
    for cam in os.listdir(os.path.join(root, 'train', seq)):
        dets_true = group_by_frame(parse_annotations_from_txt(os.path.join(root, 'train', seq, cam, 'gt', 'gt.txt')))
        dets_pred = group_by_frame(parse_annotations_from_txt(os.path.join('results', seq, cam, 'results.txt')))
        for frame in dets_true.keys():
            y_true = dets_true.get(frame, [])
            y_pred = dets_pred.get(frame, [])
            accumulator.update(y_true, y_pred)
    print(f'IDF1: {accumulator.get_idf1()}')
