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


# --- NEW: HISTORY INDEX ROUTE ---
@kitting_bp.route('/history')
def history_index():
    db = get_db()
    # Fetch all jobs that are NOT 'on-going'
    completed_jobs = list(db.activities.find({
        "status": {"$ne": "on-going"}
    }).sort("start_time", -1)) # Newest first
    
    return render_template('kitting_history.html', jobs=completed_jobs)

@kitting_bp.route('/validate_step1', methods=['POST'])
def validate_step1():
    try:
        data = request.json
        db = get_db()
        table_id = data.get('table_id')
        kit_name_input = data.get('kit_name', '').strip()
        edp_input = str(data.get('edp_number', '')).strip()

        active_activity = db.activities.find_one({"table_id": table_id, "status": "on-going"})
        if active_activity:
            return jsonify({'status': 'error', 'message': f"Table {table_id} is currently busy with Order {active_activity.get('order_number')}."})

        kit = db.kits.find_one({"kit_name": kit_name_input})
        if not kit:
            regex = re.compile(f"^{re.escape(kit_name_input)}$", re.IGNORECASE)
            kit = db.kits.find_one({"kit_name": regex})
        if not kit:
            return jsonify({'status': 'error', 'message': f"Kit '{kit_name_input}' not found in database."})

        if str(kit.get('edp_number', '')).strip() != edp_input:
            real_kit_name = kit.get('kit_name')
            return jsonify({'status': 'error', 'message': f"EDP Mismatch! Kit '{real_kit_name}' expects EDP '{kit.get('edp_number')}'."})

        return jsonify({'status': 'success', 'message': 'Validation OK'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@kitting_bp.route('/setup/step2')
def step2_placeholder():
    return "<h1>Step 2: Configuration (Coming Soon)</h1><p>Validation Passed.</p><a href='/kitting/'>Back</a>"

@kitting_bp.route('/start_activity', methods=['POST'])
def start_activity():
    try:
        data = request.json
        db = get_db()
        kit_definition = db.kits.find_one({"kit_name": data.get('kit_name', '')})
        
        if not kit_definition:
            regex = re.compile(f"^{re.escape(data.get('kit_name', ''))}$", re.IGNORECASE)
            kit_definition = db.kits.find_one({"kit_name": regex})
        
        if not kit_definition:
            return jsonify({'status': 'error', 'message': 'Kit not found.'}), 404

        raw_parts = kit_definition.get('parts', [])
        sanitized_parts = []
        for part in raw_parts:
            # Parse alerts (supports both old array and new boolean format)
            alerts_list = part.get('alerts', []) or []
            part['alert_missing'] = part.get('alert_missing', 'missing' in alerts_list)
            part['alert_undercount'] = part.get('alert_undercount', 'undercount' in alerts_list)
            part['alert_overcount'] = part.get('alert_overcount', 'overcount' in alerts_list)
            
            part['found_quantity'] = 0
            part['status'] = 'pending'
            sanitized_parts.append(part)

        new_activity = {
            "start_time": datetime.utcnow(),
            "table_id": data.get('table_id'),
            "kit_name": kit_definition.get('kit_name'), 
            "edp_number": data.get('edp_number'),
            "order_number": data.get('order_number'),
            "total_kits_to_pack": int(data.get('units', 1)),
            "current_kit_index": 1,
            "status": "on-going",
            "components": sanitized_parts, 
            "history": [],
            "current_kit_errors": [], # Initialize error list
            "last_detected_index": -1
        }
        result = db.activities.insert_one(new_activity)
        new_activity['_id'] = str(result.inserted_id) 
        new_activity['activity_id'] = str(result.inserted_id)
        new_activity['start_time'] = new_activity['start_time'].isoformat()

        socketio.emit('new_kitting_started', {"tableId": data.get('table_id'), "kittingDetails": new_activity}, to=f"table_{data.get('table_id')}")
        return jsonify({'status': 'success', 'redirect_url': url_for('kitting.monitor_activity', activity_id=new_activity['activity_id'])})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@kitting_bp.route('/monitor/<activity_id>')
def monitor_activity(activity_id):
    db = get_db()
    activity = db.activities.find_one({"_id": ObjectId(activity_id)})
    if not activity: return "Activity not found", 404
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
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
# --- NEW: IMAGE SERVER ROUTE ---
# This ensures images are accessible via network (e.g., http://192.168.1.5:5000/kitting/captures/xyz.jpg)
@kitting_bp.route('/captures/<path:filename>')
def get_image(filename):
    # Ensure Config.UPLOAD_FOLDER is absolute or correctly relative
    # For now, we assume Config.UPLOAD_FOLDER points to 'app/static/captures' or similar
    return send_from_directory(Config.UPLOAD_FOLDER, filename)



# --- HELPER: FINISH KIT LOGIC ---
def perform_kit_completion(activity, db, table_id, warning_type=None, overcount_list=None):
    current_index = activity.get('current_kit_index', 1)
    total_kits = activity.get('total_kits_to_pack', 1)
    
    # 1. Create Snapshot (Must capture errors BEFORE we wipe them)
    history_doc = {
        "kit_number": current_index,
        "completed_at": datetime.utcnow(),
        "components_snapshot": activity['components'], 
        "errors_snapshot": activity.get('current_kit_errors', []), # Saves wrong parts
        "status": "completed_with_warning" if warning_type else "completed"
    }

    # 2. Archive to 'kit_history' Collection
    history_doc["activity_id"] = activity['_id']
    db.kit_history.insert_one(history_doc)

    # 3. Archive to 'history' Array INSIDE Activity Document
    # Remove _id to clean up the embedded array
    doc_copy = history_doc.copy()
    if '_id' in doc_copy: del doc_copy['_id']
    if 'activity_id' in doc_copy: del doc_copy['activity_id']

    db.activities.update_one(
        {"_id": activity['_id']},
        {"$push": {"history": doc_copy}}
    )

    # 4. Check Job End or Move Next
    if current_index >= total_kits:
        db.activities.update_one(
            {"_id": activity['_id']},
            {"$set": {"status": "completed_job", "end_time": datetime.utcnow()}}
        )
        socketio.emit('ui_update', {"type": "job_completed", "tableId": table_id}, to=f"table_{table_id}")
    else:
        # Reset for Next Kit
        new_index = current_index + 1
        reset_components = []
        for part in activity['components']:
            part['found_quantity'] = 0
            part['status'] = 'pending'
            part.pop('sequence_order', None)
            part.pop('last_image_url', None)
            # Remove validation error notes so next kit starts fresh
            part.pop('resolution_reason', None) 
            part.pop('resolution_type', None)
            reset_components.append(part)
        
        db.activities.update_one(
            {"_id": activity['_id']},
            {
                "$set": {
                    "current_kit_index": new_index, 
                    "components": reset_components, 
                    "current_kit_errors": [], # Reset error list
                    "last_detected_index": -1
                }
            }
        )
        
        msg_type = "kit_completed_with_warning" if warning_type else "kit_completed"
        socketio.emit('ui_update', {
            "type": msg_type,
            "tableId": table_id,
            "completed_count": current_index,
            "overcount_list": overcount_list
        }, to=f"table_{table_id}")

# --- DETECTION API ---
@kitting_bp.route('/api/<table_id>/detection', methods=['POST'])
def update_detection(table_id):
    db = get_db()
    if 'image' not in request.files: return jsonify({"message": "No image"}), 422
    file = request.files['image']
    raw_payload = request.form.get('payload')
    data = json.loads(raw_payload) if raw_payload else {}
    
    cam_id = data.get('camId', '')
    detected_part = data.get('detectedPart', '')
    ai_detected_name = data.get('AiDetectedPartName', '')
    avg_threshold = data.get('avgThreshold', 0.0)
    
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404
    
    activity_id = str(activity['_id'])
    kit_name = activity.get('kit_name', 'Unknown')
    
    filename = secure_filename(f"{activity_id}_kit{activity.get('current_kit_index')}_{detected_part}_{int(datetime.utcnow().timestamp())}.jpg")
    file.save(os.path.join(Config.UPLOAD_FOLDER, filename))
    image_url = url_for('static', filename=f'captures/{filename}')

    current_components = activity.get('components', [])
    target_index = -1
    target_part = None

    # Pass 1: Hungry Slot
    for idx, part in enumerate(current_components):
        if str(part.get('name')).strip() == str(detected_part).strip() and str(part.get('camera')).strip() == str(cam_id).strip():
            if part.get('found_quantity', 0) < part.get('quantity', 1):
                target_part = part; target_index = idx; break
    
    # Pass 2: Overcount Slot
    if not target_part:
        for idx, part in enumerate(current_components):
            if str(part.get('name')).strip() == str(detected_part).strip() and str(part.get('camera')).strip() == str(cam_id).strip():
                target_part = part; target_index = idx; break

    # WRONG PART DETECTED
    if not target_part:
        # We DO NOT log to DB yet. We trigger UI. UI calls /resolve_error which logs it.
        socketio.emit('ui_update', {
            "type": "error_alert",
            "message": "wrong_part_detected",
            "imageUrl": image_url,
            "detectedPart": detected_part,
            "error_code": "wrong-part"
        }, to=f"table_{table_id}")
        return jsonify({"message": "wrong_part", "code": "wrong-part"}), 409

    # CORRECT PART
    new_found = target_part.get('found_quantity', 0) + 1
    required_qty = target_part.get('quantity', 1)
    update_field = f"components.{target_index}"
    db_updates = {
        f"{update_field}.found_quantity": new_found,
        f"{update_field}.last_image_url": image_url,
        "last_updated": datetime.utcnow(),
        "last_detected_index": target_index
    }
    if new_found >= required_qty:
        db_updates[f"{update_field}.status"] = "completed"
        if not target_part.get('sequence_order'):
            c = sum(1 for p in current_components if p.get('status') == 'completed')
            db_updates[f"{update_field}.sequence_order"] = c + 1
            
    db.activities.update_one({"_id": ObjectId(activity_id)}, {"$set": db_updates})

    socketio.emit('ui_update', {
        "type": "refresh_needed",
        "popup_data": {
            "part_name": target_part.get('name'),
            "found_qty": new_found,
            "required_qty": required_qty,
            "imageUrl": image_url
        }
    }, to=f"table_{table_id}")

    return jsonify({"message": "correct-part-detected", "found": new_found}), 200

# --- VALIDATION API ---
@kitting_bp.route('/api/<table_id>/validate_cycle', methods=['POST'])
def validate_cycle(table_id):
    db = get_db()
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404
    
    components = activity.get('components', [])
    missing_list = []
    undercount_list = []
    overcount_list = []

    for part in components:
        name = part.get('name')
        required = part.get('quantity', 0)
        found = part.get('found_quantity', 0)
        
        if found == 0 and required > 0 and part.get('alert_missing', False):
            missing_list.append(name)
        elif found > 0 and found < required and part.get('alert_undercount', False):
            undercount_list.append(name)
        elif found > required and part.get('alert_overcount', False):
            overcount_list.append(name)

    # BLOCKING ERRORS
    if missing_list or undercount_list:
        socketio.emit('ui_update', {
            "type": "validation_error",
            "missing": missing_list,
            "undercount": undercount_list,
            "overcount": overcount_list
        }, to=f"table_{table_id}")
        return jsonify({"message": "part-missing", "missingPartList": missing_list}), 200

    # SUCCESS / WARNING
    perform_kit_completion(activity, db, table_id, warning_type="overcount" if overcount_list else None, overcount_list=overcount_list)
    return jsonify({"message": "kit-completed"}), 200

# --- RESOLVE ERROR ---
@kitting_bp.route('/api/<table_id>/resolve_error', methods=['POST'])
def resolve_error(table_id):
    db = get_db()
    data = request.json
    error_type = data.get('error_type') 
    reason = data.get('reason')
    error_details = data.get('error_details', {})
    
    activity = db.activities.find_one({"table_id": str(table_id), "status": "on-going"})
    if not activity: return jsonify({"message": "No active job"}), 404

    # 1. Create Log Entry
    log_doc = {
        "activity_id": activity['_id'],
        "kit_number": activity.get('current_kit_index', 1),
        "table_id": table_id,
        "timestamp": datetime.utcnow(),
        "error_type": error_type,
        "reason_selected": reason,
        "error_details": error_details 
    }
    
    # 2. Global Log (This adds '_id' to log_doc in-place)
    db.error_logs.insert_one(log_doc)

    # --- FIX: Ensure the embedded copy uses String ID or No ID ---
    log_doc_clean = log_doc.copy()
    if '_id' in log_doc_clean: 
        log_doc_clean['_id'] = str(log_doc_clean['_id']) # Convert ObjectId to String
    if 'activity_id' in log_doc_clean:
        log_doc_clean['activity_id'] = str(log_doc_clean['activity_id'])

    # 3. Activity Log (Push cleaned data)
    db.activities.update_one(
        {"_id": activity['_id']},
        {"$push": {"current_kit_errors": log_doc_clean}}
    )

    # 4. Update Components (If Validation Error)
    if error_type == 'validation':
        components = activity.get('components', [])
        # Safe merge of lists
        missing = error_details.get('missing') or []
        undercount = error_details.get('undercount') or []
        problematic_parts = set(missing + undercount)
        
        for idx, part in enumerate(components):
            if part.get('name') in problematic_parts:
                key = f"components.{idx}"
                db.activities.update_one(
                    {"_id": activity['_id']},
                    {"$set": {
                        f"{key}.resolution_reason": reason,
                        f"{key}.resolution_type": "validation_override"
                    }}
                )

    # 5. Perform Action
    updated_activity = db.activities.find_one({"_id": activity['_id']})
    
    if error_type == 'validation':
        perform_kit_completion(updated_activity, db, table_id)
        return jsonify({"status": "success", "action": "kit_finished"})
    else:
        return jsonify({"status": "success", "action": "continue"})
    
    
    
    
    
    
    
    
    
    
    
    
    
    


# --- NEW: HISTORY DETAILS API ---
@kitting_bp.route('/api/history/<activity_id>/<int:kit_number>')
def get_kit_history_details(activity_id, kit_number):
    db = get_db()
    try:
        record = db.kit_history.find_one({
            "activity_id": ObjectId(activity_id),
            "kit_number": kit_number
        })
        
        if not record:
            return jsonify({"status": "error", "message": "Record not found"}), 404

        # --- FIX: Sanitize ObjectIds for JSON ---
        
        # 1. Sanitize Errors List
        errors = record.get('errors_snapshot', [])
        for err in errors:
            if '_id' in err: err['_id'] = str(err['_id'])
            if 'activity_id' in err: err['activity_id'] = str(err['activity_id'])
            
        # 2. Sanitize Components List (Just in case)
        components = record.get('components_snapshot', [])
        for comp in components:
            if '_id' in comp: comp['_id'] = str(comp['_id'])

        return jsonify({
            "status": "success",
            "kit_number": record.get('kit_number'),
            "completed_at": record.get('completed_at'),
            "status_label": record.get('status'),
            "components": components,
            "errors": errors
        })

    except Exception as e:
        print(f"❌ History API Error: {e}") # Debug print
        return jsonify({"status": "error", "message": str(e)}), 500
    
    
    
    
    
# --- NEW: HISTORY SUMMARY API (For the Grid View) ---
@kitting_bp.route('/api/history_summary/<activity_id>')
def get_history_summary(activity_id):
    db = get_db()
    try:
        # 1. Get the Activity to know total kits and current progress
        activity = db.activities.find_one({"_id": ObjectId(activity_id)})
        if not activity: return jsonify({"status": "error"}), 404

        total_kits = activity.get('total_kits_to_pack', 1)
        current_kit_idx = activity.get('current_kit_index', 1)
        
        # 2. Get Completed History
        # We sort by kit_number to ensure order
        history_cursor = db.kit_history.find({"activity_id": ObjectId(activity_id)}).sort("kit_number", 1)
        
        summary_map = {} # Map kit_number -> status_color
        
        for record in history_cursor:
            k_num = record.get('kit_number')
            components = record.get('components_snapshot', [])
            errors = record.get('errors_snapshot', [])
            
            # --- DETERMINE COLOR LOGIC ---
            status = 'green' # Default
            
            # Rule 1: Red if Wrong Object Detected
            if errors and len(errors) > 0:
                status = 'red'
            
            # Check Components
            else:
                has_mismatch = False
                has_missing = False
                
                for part in components:
                    found = part.get('found_quantity', 0)
                    required = part.get('quantity', 0)
                    
                    if found == 0:
                        has_missing = True
                        break # Red overrides all, stop checking
                    
                    if found != required:
                        has_mismatch = True # Potential Yellow
                
                if has_missing:
                    status = 'red' # Rule 3: Red if component missing
                elif has_mismatch:
                    status = 'yellow' # Rule 2: Yellow if under/over count
            
            summary_map[k_num] = status

        # 3. Build Final List for Grid
        grid_data = []
        for i in range(1, total_kits + 1):
            item = {"kit_number": i}
            
            if i < current_kit_idx:
                # Completed Kit
                item["state"] = "completed"
                item["color"] = summary_map.get(i, "grey") # Default grey if log missing
            elif i == current_kit_idx:
                # Current Kit
                item["state"] = "in_progress"
                item["color"] = "blue"
            else:
                # Future Kit
                item["state"] = "pending"
                item["color"] = "grey"
                
            grid_data.append(item)

        return jsonify({"status": "success", "grid": grid_data})

    except Exception as e:
        print(f"❌ Summary Error: {e}")
        return jsonify({"status": "error", "message": str(e)}), 500