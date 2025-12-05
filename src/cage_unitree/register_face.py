import cv2
import mediapipe as mp
import os
import argparse
import time

def register_face_simple(name, output_dir="faces", target_count=200):
    """
    Registers a face by automatically capturing a set number of images.
    """
    person_dir = os.path.join(output_dir, name)
    os.makedirs(person_dir, exist_ok=True)

    mp_face_detection = mp.solutions.face_detection
    mp_drawing = mp.solutions.drawing_utils

    cap = cv2.VideoCapture(0)
    if not cap.isOpened():
        print("Cannot open webcam")
        return

    print(f"Starting rapid face registration for '{name}'...")
    print(f"Target: {target_count} images")
    print("Just look at the camera and move your head slightly.")

    count = 0
    start_time = time.time()

    with mp_face_detection.FaceDetection(model_selection=0, min_detection_confidence=0.5) as face_detection:
        while count < target_count:
            success, image = cap.read()
            if not success:
                continue

            # Flip for selfie view
            image = cv2.flip(image, 1)
            img_h, img_w, _ = image.shape
            
            image_rgb = cv2.cvtColor(image, cv2.COLOR_BGR2RGB)
            results = face_detection.process(image_rgb)

            face_found = False
            face_crop = None

            if results.detections:
                # Assume single face
                detection = results.detections[0]
                
                # Draw detection
                # mp_drawing.draw_detection(image, detection)
                
                bboxC = detection.location_data.relative_bounding_box
                x = int(bboxC.xmin * img_w)
                y = int(bboxC.ymin * img_h)
                w = int(bboxC.width * img_w)
                h = int(bboxC.height * img_h)
                
                # Add margin
                margin = 50
                x = max(0, x - margin)
                y = max(0, y - margin)
                w = min(img_w - x, w + 2 * margin)
                h = min(img_h - y, h + 2 * margin)
                
                face_crop = image[y:y+h, x:x+w].copy()
                
                # Draw bounding box on screen
                cv2.rectangle(image, (x, y), (x+w, y+h), (0, 255, 0), 2)
                face_found = True

            # Auto capture
            if face_found and face_crop is not None and face_crop.size > 0:
                timestamp = int(time.time() * 1000)
                filename = f"{person_dir}/{name}_{timestamp}.jpg"
                cv2.imwrite(filename, face_crop)
                count += 1
                # print(f"Captured {count}/{target_count}")
                
                # Visual flash effect every 10 images or so? No, too fast.
                # Just a small indicator
                cv2.circle(image, (50, 50), 20, (0, 0, 255), -1)

            # UI Overlay
            # Progress Bar
            bar_width = 400
            bar_height = 30
            bar_x = (img_w - bar_width) // 2
            bar_y = img_h - 60
            
            progress = count / target_count
            cv2.rectangle(image, (bar_x, bar_y), (bar_x + bar_width, bar_y + bar_height), (100, 100, 100), -1)
            cv2.rectangle(image, (bar_x, bar_y), (bar_x + int(bar_width * progress), bar_y + bar_height), (0, 255, 0), -1)
            
            text = f"Captured: {count} / {target_count}"
            cv2.putText(image, text, (bar_x, bar_y - 15), cv2.FONT_HERSHEY_SIMPLEX, 1, (0, 255, 0), 2)
            
            cv2.imshow('Face Registration', image)
            if cv2.waitKey(5) & 0xFF == ord('q'):
                break
            
            # Small delay to prevent duplicate frames if fps is high, but we want speed.
            # time.sleep(0.01) 

    cap.release()
    cv2.destroyAllWindows()
    print(f"Done! Saved {count} images to {person_dir}")

if __name__ == "__main__":
    parser = argparse.ArgumentParser(description="Register a new face for recognition")
    parser.add_argument("name", type=str, help="Name of the person to register")
    parser.add_argument("--count", type=int, default=200, help="Number of images to capture")
    args = parser.parse_args()
    
    base_dir = os.path.dirname(os.path.abspath(__file__))
    faces_dir = os.path.join(base_dir, "faces")
    
    register_face_simple(args.name, faces_dir, args.count)
