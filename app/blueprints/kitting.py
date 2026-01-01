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

kitting_bp = Blueprint('kitting', __name__, url_prefix='/kitting')

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

@kitting_bp.route('/monitor/<activity_id>')
def monitor_activity(activity_id):
    db = get_db()
    activity = db.activities.find_one({"_id": ObjectId(activity_id)})
    if not activity: return "Not Found", 404
    return render_template('monitor.html', activity=activity)

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
    
    history_doc = {
        "kit_number": current_index,
        "camera_id": cam_id,
        "completed_at": datetime.utcnow(),
        "components_snapshot": cam_components,
        "errors_snapshot": activity.get(error_key, []),
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
    # FIX: If we passed the total, we DON'T reset to pending. We leave them as "Completed" 
    # so the backend state remains consistent, and the UI can handle the "Done" state.
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

@kitting_bp.route('/api/<table_id>/detection', methods=['POST'])
def update_detection(table_id):
    db = get_db()
    if 'image' not in request.files: return jsonify({"message": "No image"}), 422
    file = request.files['image']
    raw_payload = request.form.get('payload')
    data = json.loads(raw_payload) if raw_payload else {}
    
    cam_id = str(data.get('camId', '')).lower() 
    detected_part = data.get('detectedPart', '')
    
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404
    
    current_kit_num = activity.get(f'current_kit_index_{cam_id}', 1)
    total_kits = activity.get('total_kits_to_pack', 1)

    # FIX: Block detection if camera is already done
    if current_kit_num > total_kits:
        return jsonify({"message": "camera-job-completed", "code": "done"}), 200
    
    activity_id = str(activity['_id'])
    filename = secure_filename(f"{activity_id}_{cam_id}_kit{current_kit_num}_{detected_part}_{int(datetime.utcnow().timestamp())}.jpg")
    file.save(os.path.join(Config.UPLOAD_FOLDER, filename))
    
    # FIX: Use relative URL (remove _external=True) to avoid localhost issues on tablet
    image_url = url_for('kitting.get_image', filename=filename) 

    current_components = activity.get('components', [])
    target_index = -1
    target_part = None

    for idx, part in enumerate(current_components):
        if str(part.get('camera')).lower() == cam_id:
            if str(part.get('name')) == str(detected_part):
                if part.get('found_quantity', 0) < part.get('quantity', 1):
                    target_part = part; target_index = idx; break
    
    if not target_part:
        for idx, part in enumerate(current_components):
            if str(part.get('camera')).lower() == cam_id:
                if str(part.get('name')) == str(detected_part):
                    target_part = part; target_index = idx; break

    if not target_part:
        socketio.emit('ui_update', {
            "type": "error_alert",
            "message": "wrong_part_detected",
            "imageUrl": image_url,
            "detectedPart": detected_part,
            "error_code": "wrong-part",
            "camId": cam_id 
        }, to=f"table_{table_id}")
        return jsonify({"message": "wrong_part", "code": "wrong-part"}), 409

    new_found = target_part.get('found_quantity', 0) + 1
    required_qty = target_part.get('quantity', 1)
    
    last_detected_key = f"last_detected_index_{cam_id}" 
    
    update_field = f"components.{target_index}"
    db_updates = {
        f"{update_field}.found_quantity": new_found,
        f"{update_field}.last_image_url": image_url,
        "last_updated": datetime.utcnow(),
        last_detected_key: target_index 
    }
    
    if new_found >= required_qty:
        db_updates[f"{update_field}.status"] = "completed"
        if not target_part.get('sequence_order'):
            c_done = sum(1 for p in current_components if str(p.get('camera')).lower() == cam_id and p.get('status') == 'completed')
            db_updates[f"{update_field}.sequence_order"] = c_done + 1

    db.activities.update_one({"_id": ObjectId(activity_id)}, {"$set": db_updates})

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

    return jsonify({"message": "correct-part-detected", "found": new_found}), 200

@kitting_bp.route('/api/<table_id>/validate_cycle', methods=['POST'])
def validate_cycle(table_id):
    db = get_db()
    data = request.json
    cam_id = str(data.get('camId', '')).lower() 
    
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404
    
    components = activity.get('components', [])
    missing = []; undercount = []; overcount = []

    for part in components:
        if str(part.get('camera')).lower() == cam_id:
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
    error_type = data.get('error_type') 
    reason = data.get('reason')
    error_details = data.get('error_details', {})
    cam_id = str(error_details.get('camId', 'cam1')).lower() 
    
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404

    log_doc = {
        "activity_id": activity['_id'],
        "kit_number": activity.get(f'current_kit_index_{cam_id}', 1),
        "camera_id": cam_id,
        "table_id": table_id,
        "timestamp": datetime.utcnow(),
        "error_type": error_type,
        "reason_selected": reason,
        "error_details": error_details 
    }
    db.error_logs.insert_one(log_doc)
    
    clean_log = log_doc.copy()
    if '_id' in clean_log: clean_log['_id'] = str(clean_log['_id'])
    if 'activity_id' in clean_log: clean_log['activity_id'] = str(clean_log['activity_id'])

    error_key = f"current_kit_errors_{cam_id}"
    db.activities.update_one({"_id": activity['_id']}, {"$push": {error_key: clean_log}})

    if error_type == 'validation':
        components = activity.get('components', [])
        problems = set((error_details.get('missing') or []) + (error_details.get('undercount') or []))
        for idx, part in enumerate(components):
            if str(part.get('camera')).lower() == cam_id and part.get('name') in problems:
                key = f"components.{idx}"
                db.activities.update_one({"_id": activity['_id']}, {"$set": {
                    f"{key}.resolution_reason": reason,
                    f"{key}.resolution_type": "validation_override"
                }})

    updated_act = db.activities.find_one({"_id": activity['_id']})
    if error_type == 'validation':
        perform_camera_completion(updated_act, db, table_id, cam_id)
        return jsonify({"status": "success", "action": "kit_finished"})
    else:
        return jsonify({"status": "success", "action": "continue"})

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
        
        for e in record.get('errors_snapshot', []): 
            if '_id' in e: e['_id'] = str(e['_id'])
            if 'activity_id' in e: e['activity_id'] = str(e['activity_id'])
        
        for c in record.get('components_snapshot', []):
             if '_id' in c: c['_id'] = str(c['_id'])

        return jsonify({
            "status": "success",
            "components": record.get('components_snapshot', []),
            "errors": record.get('errors_snapshot', [])
        })
    except Exception as e: return jsonify({"status": "error"}), 500