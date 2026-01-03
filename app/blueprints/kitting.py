from flask import Blueprint, render_template, request, jsonify, url_for, current_app, send_from_directory
from app.db import get_db
import re
from bson.objectid import ObjectId
from app.socket_events import socketio
from datetime import datetime
import os
import json
from werkzeug.utils import secure_filename
from app.config import Config

import logging
import traceback

from bson.errors import InvalidId # Import at top

kitting_bp = Blueprint('kitting', __name__, url_prefix='/kitting')

# --- HELPER: NORMALIZE CAM ID ---
def get_safe_cam_id(input_id):
    """Ensures inputs like '1', 'Camera 1', 'CAM1' always return 'cam1'."""
    s = str(input_id).lower().strip()
    if '1' in s: return 'cam1'
    if '2' in s: return 'cam2'
    return 'cam1' # Default fallback

@kitting_bp.route('/')
def index():
    db = get_db()
    active_jobs = list(db.activities.find({"status": "on-going"}))
    return render_template('kitting.html', active_jobs=active_jobs)

@kitting_bp.route('/history')
def history_index():
    db = get_db()
    completed_jobs = list(db.activities.find({
        "status": {"$ne": "on-going"}
    }).sort("start_time", -1))
    return render_template('kitting_history.html', jobs=completed_jobs)

@kitting_bp.route('/validate_step1', methods=['POST'])
def validate_step1():
    try:
        data = request.json
        db = get_db()
        table_id = data.get('table_id')
        kit_name_input = data.get('kit_name', '').strip()
        edp_input = str(data.get('edp_number', '')).strip()

        active = db.activities.find_one({"table_id": table_id, "status": "on-going"})
        if active: return jsonify({'status': 'error', 'message': f"Table {table_id} is busy."})

        kit = db.kits.find_one({"kit_name": kit_name_input})
        if not kit:
            regex = re.compile(f"^{re.escape(kit_name_input)}$", re.IGNORECASE)
            kit = db.kits.find_one({"kit_name": regex})
        if not kit: return jsonify({'status': 'error', 'message': f"Kit '{kit_name_input}' not found."})

        if str(kit.get('edp_number', '')).strip() != edp_input:
            return jsonify({'status': 'error', 'message': "EDP Mismatch!"})

        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@kitting_bp.route('/setup/step2')
def step2_placeholder(): return "Step 2"

@kitting_bp.route('/start_activity', methods=['POST'])
def start_activity():
    try:
        data = request.json
        db = get_db()
        kit_def = db.kits.find_one({"kit_name": data.get('kit_name', '')})
        if not kit_def:
            regex = re.compile(f"^{re.escape(data.get('kit_name', ''))}$", re.IGNORECASE)
            kit_def = db.kits.find_one({"kit_name": regex})
        if not kit_def: return jsonify({'status': 'error', 'message': 'Kit not found'}), 404

        raw_parts = kit_def.get('parts', [])
        sanitized_parts = []
        for part in raw_parts:
            alerts = part.get('alerts', []) or []
            part['alert_missing'] = part.get('alert_missing', 'missing' in alerts)
            part['alert_undercount'] = part.get('alert_undercount', 'undercount' in alerts)
            part['alert_overcount'] = part.get('alert_overcount', 'overcount' in alerts)
            part['found_quantity'] = 0
            part['status'] = 'pending'
            sanitized_parts.append(part)

        new_activity = {
            "start_time": datetime.utcnow(),
            "table_id": data.get('table_id'),
            "kit_name": kit_def.get('kit_name'), 
            "edp_number": data.get('edp_number'),
            "order_number": data.get('order_number'),
            "total_kits_to_pack": int(data.get('units', 1)),
            "current_kit_index_cam1": 1,
            "current_kit_index_cam2": 1,
            "status": "on-going",
            "components": sanitized_parts, 
            "history": [],
            "current_kit_errors_cam1": [], 
            "current_kit_errors_cam2": [],
            "last_detected_index_cam1": -1,
            "last_detected_index_cam2": -1
        }
        
        result = db.activities.insert_one(new_activity)
        new_activity['_id'] = str(result.inserted_id) 
        new_activity['activity_id'] = str(result.inserted_id)
        new_activity['start_time'] = new_activity['start_time'].isoformat()
        
        socketio.emit('new_kitting_started', {"tableId": data.get('table_id'), "kittingDetails": new_activity}, to=f"table_{data.get('table_id')}")

        return jsonify({'status': 'success', 'redirect_url': url_for('kitting.monitor_activity', activity_id=str(result.inserted_id))})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500
    
# --- NEW: HELPER FOR SANITIZATION ---
def sanitize_activity_for_json(activity):
    """
    Recursively converts ObjectId and datetime to strings so they
    can be safely rendered into JavaScript variables in the template.
    """
    if not activity: return {}
    
    # 1. Convert main ID
    if '_id' in activity:
        activity['_id'] = str(activity['_id'])
    
    # 2. Convert Start Time
    if 'start_time' in activity and isinstance(activity['start_time'], datetime):
        activity['start_time'] = activity['start_time'].isoformat()

    # 3. Helper to clean a list of errors
    def clean_error_list(error_list):
        if not error_list: return []
        cleaned = []
        for err in error_list:
            # Convert timestamp
            if 'timestamp' in err and isinstance(err['timestamp'], datetime):
                err['timestamp'] = err['timestamp'].isoformat()
            # Convert any nested ObjectIds (just in case)
            if '_id' in err: err['_id'] = str(err['_id'])
            cleaned.append(err)
        return cleaned

    # 4. Clean the specific error lists used by JS
    if 'current_kit_errors_cam1' in activity:
        activity['current_kit_errors_cam1'] = clean_error_list(activity['current_kit_errors_cam1'])
    
    if 'current_kit_errors_cam2' in activity:
        activity['current_kit_errors_cam2'] = clean_error_list(activity['current_kit_errors_cam2'])

    return activity

@kitting_bp.route('/monitor/<activity_id>')
def monitor_activity(activity_id):
    db = get_db()
    
    # 1. Validate ID format
    try:
        oid = ObjectId(activity_id)
    except InvalidId:
        # Instead of crashing, return a helpful error or redirect back to history
        current_app.logger.error(f"Invalid Activity ID received: {activity_id}")
        return render_template('error.html', message="Invalid Job ID format"), 400

    # 2. Find Activity
    activity = db.activities.find_one({"_id": oid})
    if not activity: 
        return "Activity Not Found", 404
    
    sanitized_activity = sanitize_activity_for_json(activity)
    return render_template('monitor.html', activity=sanitized_activity)

@kitting_bp.route('/complete_manual', methods=['POST'])
def complete_manual():
    try:
        data = request.json
        db = get_db()
        db.activities.update_one(
            {"_id": ObjectId(data.get('activity_id'))},
            {"$set": { "status": "completed-manually", "end_time": datetime.utcnow() }}
        )
        return jsonify({'status': 'success'})
    except Exception as e: return jsonify({'status': 'error', 'message': str(e)}), 500

@kitting_bp.route('/captures/<path:filename>')
def get_image(filename):
    return send_from_directory(Config.UPLOAD_FOLDER, filename)

# --- HELPER: FINISH KIT (PER CAMERA) ---
def perform_camera_completion(activity, db, table_id, cam_id, warning_type=None):
    index_key = f"current_kit_index_{cam_id}"
    error_key = f"current_kit_errors_{cam_id}"
    last_detected_key = f"last_detected_index_{cam_id}"
    
    current_index = activity.get(index_key, 1)
    total_kits = activity.get('total_kits_to_pack', 1)
    
    # 1. Archive
    cam_components = [p for p in activity['components'] if str(p.get('camera')).lower() == cam_id.lower()]
    
    # --- FIX: QUERY USING OBJECTID ---
    # Do not convert activity['_id'] to string here. 
    # MongoDB error_logs has activity_id as ObjectId.
    logged_errors = list(db.error_logs.find({
        "activity_id": activity['_id'],  # Pass ObjectId directly
        "kit_number": current_index,
        "camera_id": cam_id
    }))
    
    # Clean up _id from the fetched logs to avoid duplication issues in snapshot
    for err in logged_errors:
        if '_id' in err: del err['_id'] # Remove the log's own ID, we just want the data

    history_doc = {
        "kit_number": current_index,
        "camera_id": cam_id,
        "completed_at": datetime.utcnow(),
        "components_snapshot": cam_components,
        "errors_snapshot": logged_errors, # Now this will actually have data!
        "status": "completed_with_warning" if warning_type else "completed"
    }
    
    history_doc["activity_id"] = activity['_id']
    db.kit_history.insert_one(history_doc)
    
    doc_copy = history_doc.copy()
    if '_id' in doc_copy: del doc_copy['_id']
    if 'activity_id' in doc_copy: del doc_copy['activity_id']
    db.activities.update_one({"_id": activity['_id']}, {"$push": {"history": doc_copy}})

    # 2. Advance Index
    new_index = current_index + 1
    
    # 3. Reset Components (ONLY IF there is a next kit)
    if new_index <= total_kits:
        for idx, part in enumerate(activity['components']):
            if str(part.get('camera')).lower() == cam_id.lower():
                field_base = f"components.{idx}"
                db.activities.update_one(
                    {"_id": activity['_id']},
                    {"$set": {
                        f"{field_base}.found_quantity": 0,
                        f"{field_base}.status": "pending"
                    }, "$unset": {
                        f"{field_base}.sequence_order": "",
                        f"{field_base}.last_image_url": "",
                        f"{field_base}.captured_images": "", 
                        f"{field_base}.resolution_reason": "",
                        f"{field_base}.resolution_type": ""
                    }}
                )

    # 4. Update Activity Level
    db.activities.update_one(
        {"_id": activity['_id']},
        {
            "$set": {
                index_key: new_index, 
                error_key: [],
                last_detected_key: -1 
            }
        }
    )

    # 5. Check Global Job Completion
    updated_act = db.activities.find_one({"_id": activity['_id']})
    idx1 = updated_act.get('current_kit_index_cam1', 1)
    idx2 = updated_act.get('current_kit_index_cam2', 1)
    
    parts1 = [p for p in updated_act['components'] if str(p.get('camera')).lower() == 'cam1']
    parts2 = [p for p in updated_act['components'] if str(p.get('camera')).lower() == 'cam2']
    
    cam1_done = (idx1 > total_kits) or (len(parts1) == 0)
    cam2_done = (idx2 > total_kits) or (len(parts2) == 0)

    if cam1_done and cam2_done:
        db.activities.update_one({"_id": activity['_id']}, {"$set": {"status": "completed_job", "end_time": datetime.utcnow()}})
        socketio.emit('ui_update', {"type": "job_completed"}, to=f"table_{table_id}")
    else:
        socketio.emit('ui_update', {
            "type": "kit_completed" if not warning_type else "kit_completed_with_warning",
            "camId": cam_id,
            "completed_count": current_index
        }, to=f"table_{table_id}")

# --- SYSTEM STATUS API ---
@kitting_bp.route('/api/<table_id>/status', methods=['GET'])
def check_table_status(table_id):
    db = get_db()
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    
    if not activity:
        return jsonify({"status": "idle", "message": "No active job"}), 200

    errors_c1 = activity.get('current_kit_errors_cam1', [])
    errors_c2 = activity.get('current_kit_errors_cam2', [])
    
    if errors_c1 or errors_c2:
        return jsonify({
            "status": "locked", 
            "message": "Red Screen Active - Waiting for operator resolution",
            "errors": {"cam1": len(errors_c1), "cam2": len(errors_c2)}
        }), 200
        
    return jsonify({"status": "active", "message": "System ready"}), 200

# --- DETECTION API ---
import logging
import traceback
from flask import current_app

# --- DETECTION API ---
@kitting_bp.route('/api/<table_id>/detection', methods=['POST'])
def update_detection(table_id):
    """
    Handles object detection events from the AI Station.
    - Processes the image and metadata.
    - Updates kit progress if the part is valid.
    - Locks the system if the part is wrong.
    - Returns 200 (OK), 409 (Wrong Part), 423 (Locked), or 500 (Server Error).
    """
    try:
        db = get_db()
        
        # ---------------------------------------------------------------------
        # [BLOCK 1] VALIDATION & STATE CHECKS
        # ---------------------------------------------------------------------
        # 1. Fetch Active Activity
        # We look for a job on this specific table that is currently 'on-going'.
        activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
        
        if not activity:
            current_app.logger.warning(f"Detection received for inactive Table {table_id}")
            return jsonify({"message": "No active job"}), 404

        # 2. Global Lock Check
        # If any camera currently has an unresolved error (Red Screen), reject new detections.
        # This prevents the operator from continuing without resolving the issue first.
        if activity.get('current_kit_errors_cam1') or activity.get('current_kit_errors_cam2'):
            return jsonify({"message": "System Locked", "code": "system_locked"}), 423

        # 3. Input Validation
        # Ensure an image file was actually sent in the request.
        if 'image' not in request.files:
            return jsonify({"message": "No image provided"}), 422
        
        file = request.files['image']

        # ---------------------------------------------------------------------
        # [BLOCK 2] DATA PARSING & METADATA EXTRACTION
        # ---------------------------------------------------------------------
        # 4. Parse Rich Payload
        # The AI sends metadata as a JSON string inside the 'payload' form-field.
        raw_payload = request.form.get('payload')
        data = json.loads(raw_payload) if raw_payload else {}
        
        # Extract Metadata safely with defaults
        cam_id = get_safe_cam_id(data.get('camId', ''))
        detected_part = data.get('detectedPart', '')           # Logical name (mapped)
        ai_raw_name = data.get('AiDetectedPartName', '')       # Raw class name from model
        confidence = data.get('avgThreshold', 0.0)             # Confidence score (0.0 - 1.0)
        tracking_id = data.get('Tracking_id', None)            # Unique ID from object tracker
        
        # Check Job Limits
        current_kit_num = activity.get(f'current_kit_index_{cam_id}', 1)
        total_kits = activity.get('total_kits_to_pack', 1)

        # If the job is already finished for this camera, ignore extra detections.
        if current_kit_num > total_kits:
            return jsonify({"message": "camera-job-completed", "code": "done"}), 200
        
        # ---------------------------------------------------------------------
        # [BLOCK 3] FILE HANDLING
        # ---------------------------------------------------------------------
        # Generate a unique filename: {ActivityID}_{CamID}_{KitNum}_{Part}_{Timestamp}.jpg
        activity_id = str(activity['_id'])
        timestamp = int(datetime.utcnow().timestamp())
        filename = secure_filename(f"{activity_id}_{cam_id}_kit{current_kit_num}_{detected_part}_{timestamp}.jpg")
        
        # Save to disk
        file_path = os.path.join(Config.UPLOAD_FOLDER, filename)
        file.save(file_path)
        
        # Generate Web URL for the frontend to access this image
        image_url = url_for('kitting.get_image', filename=filename) 

        # Create a Rich Detection Record Object
        # This object contains all metadata to be stored in the DB (for history/debugging)
        detection_record = {
            "image_url": image_url,
            "timestamp": datetime.utcnow(),
            "ai_class_name": ai_raw_name,
            "confidence": confidence,
            "tracking_id": tracking_id,
            "cam_id": cam_id
        }

        # ---------------------------------------------------------------------
        # [BLOCK 4] PART MATCHING LOGIC
        # ---------------------------------------------------------------------
        current_components = activity.get('components', [])
        target_index = -1
        target_part = None

        # Logic A: "Hungry Slot"
        # Look for a part that MATCHES the detected name AND still NEEDS items (found < quantity).
        for idx, part in enumerate(current_components):
            if get_safe_cam_id(part.get('camera')) == cam_id:
                if str(part.get('name')) == str(detected_part):
                    if part.get('found_quantity', 0) < part.get('quantity', 1):
                        target_part = part
                        target_index = idx
                        break
        
        # Logic B: "Overcount Slot" (Fallback)
        # If all slots are full, but the part name matches, assign it to the first matching slot.
        # This allows the system to register an "Overcount" later.
        if not target_part:
            for idx, part in enumerate(current_components):
                if get_safe_cam_id(part.get('camera')) == cam_id:
                    if str(part.get('name')) == str(detected_part):
                        target_part = part
                        target_index = idx
                        break

        # ---------------------------------------------------------------------
        # [BLOCK 5] WRONG PART DETECTED (ERROR FLOW)
        # ---------------------------------------------------------------------
        # If target_part is still None, it means this detected object is not in the Kit Bill of Materials.
        if not target_part:
            error_data = {
                "error_type": "detection",
                "reason_selected": None, # Will be filled by operator resolution
                "timestamp": datetime.utcnow(),
                "error_details": {
                    "message": "wrong_part_detected",
                    "imageUrl": image_url,
                    "detectedPart": detected_part,
                    "AiDetectedPartName": ai_raw_name, # Stored for debug
                    "avgThreshold": confidence,        # Stored for debug
                    "Tracking_id": tracking_id,        # Stored for debug
                    "error_code": "wrong-part",
                    "camId": cam_id 
                }
            }
            
            # DB Action: Push error to 'current_kit_errors' array.
            # This immediately locks the system (see Block 1, Step 2).
            error_key = f"current_kit_errors_{cam_id}"
            db.activities.update_one(
                {"_id": activity['_id']}, 
                {"$push": {error_key: error_data}}
            )

            # Socket Action: Trigger Red Screen on UI
            socketio.emit('ui_update', {
                "type": "error_alert",
                "message": "wrong_part_detected",
                "imageUrl": image_url,
                "detectedPart": detected_part,
                "camId": cam_id 
            }, to=f"table_{table_id}")
            
            current_app.logger.info(f"Wrong Part Detected on Table {table_id}: {detected_part}")
            return jsonify({"message": "wrong_part", "code": "wrong-part"}), 409

        # ---------------------------------------------------------------------
        # [BLOCK 6] CORRECT PART DETECTED (SUCCESS FLOW)
        # ---------------------------------------------------------------------
        new_found = target_part.get('found_quantity', 0) + 1
        required_qty = target_part.get('quantity', 1)
        
        # Prepare DB Update
        update_field = f"components.{target_index}"
        last_detected_key = f"last_detected_index_{cam_id}"
        
        db_updates = {
            f"{update_field}.found_quantity": new_found,
            f"{update_field}.last_image_url": image_url, # Quick access for UI thumbnail
            "last_updated": datetime.utcnow(),
            last_detected_key: target_index 
        }
        
        # Check if this specific component slot is now full
        if new_found >= required_qty:
            db_updates[f"{update_field}.status"] = "completed"
            
            # Assign sequence order (e.g., this is the 3rd unique part finished)
            if not target_part.get('sequence_order'):
                c_done = sum(1 for p in current_components if get_safe_cam_id(p.get('camera')) == cam_id and p.get('status') == 'completed')
                db_updates[f"{update_field}.sequence_order"] = c_done + 1

        # DB Action: Update counts AND Push the Rich Object to history list
        db.activities.update_one(
            {"_id": ObjectId(activity_id)}, 
            {
                "$set": db_updates,
                "$push": {f"{update_field}.captured_images": detection_record} 
            }
        )

        # Socket Action: Show Green "Detected" Popup on UI
        socketio.emit('ui_update', {
            "type": "refresh_needed",
            "popup_data": {
                "part_name": target_part.get('name'),
                "found_qty": new_found,
                "required_qty": required_qty,
                "imageUrl": image_url,
                "camId": cam_id
            }
        }, to=f"table_{table_id}")

        return jsonify({
        "message": "correct-part-detected",
        "found": new_found,
        "part_name": detected_part,
        "cam_id": cam_id,
        "tracking_id": tracking_id,
        "avg_threshold": confidence
            }), 200

    # ---------------------------------------------------------------------
    # [BLOCK 7] EXCEPTION HANDLING
    # ---------------------------------------------------------------------
    except Exception as e:
        # Log the full traceback so developers can debug the crash
        error_msg = str(e)
        tb = traceback.format_exc()
        current_app.logger.error(f"CRITICAL ERROR in detection API for Table {table_id}: {error_msg}\n{tb}")
        
        # Return a 500 error so the AI station knows the server failed
        return jsonify({
            "status": "error", 
            "message": "Internal Server Error processing detection",
            "debug_error": error_msg 
        }), 500

# --- VALIDATION API ---
@kitting_bp.route('/api/<table_id>/validate_cycle', methods=['POST'])
def validate_cycle(table_id):
    db = get_db()
    data = request.json
    
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404
    
    if activity.get('current_kit_errors_cam1') or activity.get('current_kit_errors_cam2'):
        return jsonify({"message": "System Locked: Resolve Red Screen first"}), 423

    cam_id = get_safe_cam_id(data.get('camId', ''))
    
    components = activity.get('components', [])
    missing = []; undercount = []; overcount = []

    for part in components:
        if get_safe_cam_id(part.get('camera')) == cam_id:
            name = part.get('name')
            req = part.get('quantity', 0)
            found = part.get('found_quantity', 0)
            if found == 0 and req > 0 and part.get('alert_missing'): missing.append(name)
            elif found > 0 and found < req and part.get('alert_undercount'): undercount.append(name)
            elif found > req and part.get('alert_overcount'): overcount.append(name)

    if missing or undercount:
        socketio.emit('ui_update', {
            "type": "validation_error",
            "missing": missing, "undercount": undercount, "overcount": overcount,
            "camId": cam_id
        }, to=f"table_{table_id}")
        return jsonify({"message": "part-missing"}), 200

    perform_camera_completion(activity, db, table_id, cam_id, warning_type="overcount" if overcount else None)
    return jsonify({"message": "kit-completed"}), 200

@kitting_bp.route('/api/<table_id>/resolve_error', methods=['POST'])
def resolve_error(table_id):
    db = get_db()
    data = request.json
    
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404

    # Determine cam_id safely
    error_details = data.get('error_details', {})
    raw_cam = error_details.get('camId', '')
    if raw_cam: 
        cam_id = get_safe_cam_id(raw_cam)
    else:
        # Fallback check
        if activity.get('current_kit_errors_cam1'): cam_id = 'cam1'
        elif activity.get('current_kit_errors_cam2'): cam_id = 'cam2'
        else: cam_id = 'cam1'

    error_key = f"current_kit_errors_{cam_id}"
    
    log_doc = {
        "activity_id": activity['_id'],
        "kit_number": activity.get(f'current_kit_index_{cam_id}', 1),
        "camera_id": cam_id,
        "table_id": table_id,
        "timestamp": datetime.utcnow(),
        "error_type": data.get('error_type'),
        "reason_selected": data.get('reason'),
        "error_details": error_details 
    }
    db.error_logs.insert_one(log_doc)

    db.activities.update_one(
        {"_id": activity['_id']}, 
        {"$set": {error_key: []}}
    )

    if data.get('error_type') == 'validation':
        components = activity.get('components', [])
        problems = set((error_details.get('missing') or []) + (error_details.get('undercount') or []))
        for idx, part in enumerate(components):
            if get_safe_cam_id(part.get('camera')) == cam_id and part.get('name') in problems:
                key = f"components.{idx}"
                db.activities.update_one({"_id": activity['_id']}, {"$set": {
                    f"{key}.resolution_reason": data.get('reason'),
                    f"{key}.resolution_type": "validation_override"
                }})
        
        updated_act = db.activities.find_one({"_id": activity['_id']})
        perform_camera_completion(updated_act, db, table_id, cam_id)

    # --- BROADCAST RESOLUTION ---
    socketio.emit('ui_update', {
        "type": "error_resolved",
        "camId": cam_id
    }, to=f"table_{table_id}")

    return jsonify({"status": "success", "action": "resolved"})

# ... (History routes remain same) ...
@kitting_bp.route('/api/history_summary/<activity_id>/<cam_id>')
def get_history_summary(activity_id, cam_id):
    db = get_db()
    try:
        activity = db.activities.find_one({"_id": ObjectId(activity_id)})
        if not activity: return jsonify({"status": "error"}), 404
        total_kits = activity.get('total_kits_to_pack', 1)
        current_idx = activity.get(f'current_kit_index_{cam_id}', 1)
        history_cursor = db.kit_history.find({
            "activity_id": ObjectId(activity_id),
            "camera_id": cam_id
        }).sort("kit_number", 1)
        summary_map = {}
        for record in history_cursor:
            status = 'green'
            if record.get('errors_snapshot'): status = 'red'
            else:
                for part in record.get('components_snapshot', []):
                    if part['found_quantity'] == 0: status = 'red'; break
                    if part['found_quantity'] != part['quantity']: status = 'yellow'
            summary_map[record['kit_number']] = status
        grid_data = []
        for i in range(1, total_kits + 1):
            item = {"kit_number": i}
            if i < current_idx:
                item["state"] = "completed"
                item["color"] = summary_map.get(i, "grey")
            elif i == current_idx:
                item["state"] = "in_progress"
                item["color"] = "blue"
            else:
                item["state"] = "pending"
                item["color"] = "grey"
            grid_data.append(item)
        return jsonify({"status": "success", "grid": grid_data})
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500

# --- HISTORY DETAILS API (Ensure serialization) ---
@kitting_bp.route('/api/history/<activity_id>/<cam_id>/<int:kit_number>')
def get_kit_history_details(activity_id, cam_id, kit_number):
    db = get_db()
    try:
        record = db.kit_history.find_one({
            "activity_id": ObjectId(activity_id),
            "camera_id": cam_id,
            "kit_number": kit_number
        })
        if not record: return jsonify({"status": "error"}), 404
        
        # Sanitize Errors
        cleaned_errors = []
        for e in record.get('errors_snapshot', []): 
            if '_id' in e: e['_id'] = str(e['_id'])
            if 'activity_id' in e: e['activity_id'] = str(e['activity_id'])
            if 'timestamp' in e and isinstance(e['timestamp'], datetime):
                e['timestamp'] = e['timestamp'].isoformat()
            cleaned_errors.append(e)
        
        # Sanitize Components
        for c in record.get('components_snapshot', []):
             if '_id' in c: c['_id'] = str(c['_id'])

        return jsonify({
            "status": "success",
            "components": record.get('components_snapshot', []),
            "errors": cleaned_errors 
        })
    except Exception as e: return jsonify({"status": "error", "message": str(e)}), 500