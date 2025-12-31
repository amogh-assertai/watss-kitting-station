from flask import Blueprint, render_template, request, jsonify, url_for ,current_app
from app.db import get_db
import re

from bson.objectid import ObjectId

# Import socketio to send messages from Server -> AI
from app.socket_events import socketio

from datetime import datetime

kitting_bp = Blueprint('kitting', __name__, url_prefix='/kitting')

@kitting_bp.route('/')
def index():
    db = get_db()
    # Fetch all activities that are currently running
    active_jobs = list(db.activities.find({"status": "on-going"}))
    return render_template('kitting.html', active_jobs=active_jobs)

@kitting_bp.route('/validate_step1', methods=['POST'])
def validate_step1():
    """
    Validates Step 1 of creating a new activity.
    Checks:
    1. Table is not currently busy.
    2. Kit exists (Robust Search).
    3. EDP Number matches the Kit.
    """
    try:
        data = request.json
        db = get_db()
        
        table_id = data.get('table_id')
        kit_name_input = data.get('kit_name', '').strip()
        edp_input = str(data.get('edp_number', '')).strip()

        # --- Validation 1: Check Table Availability ---
        # We check for "on-going" status to match start_activity logic
        active_activity = db.activities.find_one({
            "table_id": table_id,
            "status": "on-going"
        })

        if active_activity:
            return jsonify({
                'status': 'error', 
                'message': f"Table {table_id} is currently busy with Order {active_activity.get('order_number')}."
            })

        # --- Validation 2: Check Kit Existence (Robust Search) ---
        # 1. Exact Match
        kit = db.kits.find_one({"kit_name": kit_name_input})

        # 2. Case-Insensitive Match (if exact fails)
        if not kit:
            regex = re.compile(f"^{re.escape(kit_name_input)}$", re.IGNORECASE)
            kit = db.kits.find_one({"kit_name": regex})

        if not kit:
            return jsonify({'status': 'error', 'message': f"Kit '{kit_name_input}' not found in database."})

        # --- Validation 3: Check EDP Match ---
        # Convert DB value to string for safe comparison
        db_edp = str(kit.get('edp_number', '')).strip()

        if db_edp != edp_input:
            real_kit_name = kit.get('kit_name')
            return jsonify({
                'status': 'error', 
                'message': f"EDP Mismatch! Kit '{real_kit_name}' expects EDP '{db_edp}', but you entered '{edp_input}'."
            })

        # --- Success ---
        # No redirect needed here. Frontend just wants 'success' to move to Step 2 (Handshake)
        return jsonify({'status': 'success', 'message': 'Validation OK'})

    except Exception as e:
        print(f"❌ Validation Error: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500

@kitting_bp.route('/setup/step2')
def step2_placeholder():
    """Placeholder for the next page you mentioned."""
    return "<h1>Step 2: Configuration (Coming Soon)</h1><p>Validation Passed.</p><a href='/kitting/'>Back</a>"

@kitting_bp.route('/start_activity', methods=['POST'])
def start_activity():
    try:
        data = request.json
        db = get_db()
        
        # Inputs
        kit_name_input = data.get('kit_name', '').strip()
        table_id = data.get('table_id')

        print(f"🔍 Searching for Kit: '{kit_name_input}' in 'kits' collection...")

        # 1. SEARCH IN CORRECT COLLECTION ('kits')
        kit_definition = db.kits.find_one({"kit_name": kit_name_input})
        
        if not kit_definition:
            regex = re.compile(f"^{re.escape(kit_name_input)}$", re.IGNORECASE)
            kit_definition = db.kits.find_one({"kit_name": regex})

        if not kit_definition:
            print(f"❌ Kit '{kit_name_input}' NOT found in DB.")
            return jsonify({'status': 'error', 'message': f'Kit "{kit_name_input}" not found.'}), 404

        print(f"✅ Found Kit: {kit_definition.get('kit_name')}")

        # 2. EXTRACT PARTS LIST
        parts_list = kit_definition.get('parts', [])
        
        # 3. CREATE ACTIVITY
        new_activity = {
            "start_time": datetime.utcnow(),
            "table_id": table_id,
            "kit_name": kit_definition.get('kit_name'), 
            "edp_number": data.get('edp_number'),
            "order_number": data.get('order_number'),
            "total_kits_to_pack": int(data.get('units', 1)),
            "current_kit_index": 0,
            "status": "on-going",
            "components": parts_list, 
            "history": []
        }

        # 4. SAVE TO DB
        # This adds an '_id' field of type ObjectId to new_activity automatically!
        result = db.activities.insert_one(new_activity)
        
        # --- FIX STARTS HERE ---
        # We must overwrite the ObjectId with a string so JSON doesn't crash
        new_activity['_id'] = str(result.inserted_id) 
        new_activity['activity_id'] = str(result.inserted_id)
        new_activity['start_time'] = new_activity['start_time'].isoformat()
        # --- FIX ENDS HERE ---

        # 5. SEND TO AI
        room = f"table_{table_id}"
        socket_payload = {
            "message": "new-kitting-started",
            "tableId": table_id,
            "kittingDetails": new_activity # Now safe because _id is a string
        }
        
        print(f"🚀 Sending Start Command to {room}...")
        socketio.emit('new_kitting_started', socket_payload, to=room)

        return jsonify({
            'status': 'success',
            'message': 'Job started successfully',
            'redirect_url': url_for('kitting.monitor_activity', activity_id=new_activity['activity_id'])
        })

    except Exception as e:
        print(f"❌ Error starting activity: {e}")
        return jsonify({'status': 'error', 'message': str(e)}), 500
    
    
@kitting_bp.route('/monitor/<activity_id>')
def monitor_activity(activity_id):
    """
    Placeholder for the next screen (Monitor)
    """
    db = get_db()
    activity = db.activities.find_one({"_id": ObjectId(activity_id)})
    if not activity:
        return "Activity not found", 404
    return render_template('monitor.html', activity=activity)




@kitting_bp.route('/complete_manual', methods=['POST'])
def complete_manual():
    try:
        data = request.json
        activity_id = data.get('activity_id')
        db = get_db()
        
        # Updates status to 'completed-manually'
        db.activities.update_one(
            {"_id": ObjectId(activity_id)},
            {"$set": {
                "status": "completed-manually", 
                "end_time": datetime.utcnow()
            }}
        )
        
        return jsonify({'status': 'success', 'message': 'Activity marked as completed.'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500