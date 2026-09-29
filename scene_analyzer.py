import os
from scenedetect import open_video, SceneManager, ContentDetector

def extract_smart_scenes(video_path):
    """
    Extract scenes from a video using PySceneDetect.
    Filters scenes to keep only those with a duration >= 2.0s.
    """
    print(f"[+] Dang phan tich canhlighlight tu: {video_path}...")
    try:
        video = open_video(video_path)
        scene_manager = SceneManager()
        scene_manager.add_detector(ContentDetector(threshold=27.0))
        scene_manager.detect_scenes(video)
        scene_list = scene_manager.get_scene_list()
        
        valid_scenes = []
        for scene in scene_list:
            start_time = scene[0].get_seconds()
            end_time = scene[1].get_seconds()
            duration = end_time - start_time
            
            # Giữ lại toàn bộ các cảnh có thời lượng >= 2.0 giây (Bao gồm cả cảnh siêu dài của Gameplay)
            if duration >= 2.0:
                valid_scenes.append({
                    "start": start_time,
                    "end": end_time,
                    "duration": duration,
                    "mid": start_time + duration / 2.0
                })
                
        print(f"[+] Da tim thay {len(valid_scenes)} canh highlight hop le.")
        return valid_scenes
    except Exception as e:
        print(f"[!] Loi khi phan tich canh: {e}")
        return []
