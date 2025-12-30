from flask import Blueprint, render_template, request, jsonify, url_for
from app.db import get_db

kitting_bp = Blueprint('kitting', __name__, url_prefix='/kitting')

@kitting_bp.route('/')
def index():
    """Renders the main Kitting Activities Dashboard."""
    return render_template('kitting.html')

@kitting_bp.route('/validate_step1', methods=['POST'])
def validate_step1():
    """
    Validates Step 1 of creating a new activity.
    Checks:
    1. Kit exists and EDP matches.
    2. Table is not currently busy.
    """
    try:
        data = request.json
        db = get_db()
        
        table_id = data.get('table_id')
        kit_name = data.get('kit_name')
        edp_number = data.get('edp_number')

        # --- Validation 1: Check Kit & EDP Match ---
        # Find a kit that has BOTH this name and this EDP
        kit = db.kits.find_one({
            "kit_name": kit_name, 
            "edp_number": edp_number
        })

        if not kit:
            # Check if it failed because of mismatch or non-existence
            kit_exists = db.kits.find_one({"kit_name": kit_name})
            edp_exists = db.kits.find_one({"edp_number": edp_number})

            if kit_exists:
                return jsonify({'status': 'error', 'message': f"EDP Number does not match Kit Name '{kit_name}'."})
            elif edp_exists:
                return jsonify({'status': 'error', 'message': f"Kit Name does not match EDP Number '{edp_number}'."})
            else:
                return jsonify({'status': 'error', 'message': "Kit Name and EDP Number not found in database."})

        # --- Validation 2: Check Table Availability ---
        # Check if there is an 'active' activity for this table
        # We assume active activities have status='in_progress'
        active_activity = db.activities.find_one({
            "table_id": table_id,
            "status": "in_progress"
        })

        if active_activity:
            return jsonify({'status': 'error', 'message': f"Table {table_id} is currently busy with another activity."})

        # --- Success ---
        # We return a redirect URL for the next page (which we will build later)
        # For now, we pass the data as query params or prepare a session (simplified here)
        return jsonify({
            'status': 'success', 
            'redirect': url_for('kitting.step2_placeholder', 
                                table_id=table_id, 
                                kit_id=str(kit['_id']),
                                order_number=data.get('order_number'),
                                units=data.get('units'))
        })

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@kitting_bp.route('/setup/step2')
def step2_placeholder():
    """Placeholder for the next page you mentioned."""
    return "<h1>Step 2: Configuration (Coming Soon)</h1><p>Validation Passed.</p><a href='/kitting/'>Back</a>"