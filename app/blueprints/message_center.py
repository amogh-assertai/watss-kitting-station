from flask import Blueprint, render_template, request, jsonify, redirect, url_for, session
from app import socketio, db  # Assuming db is your mongo instance from app.db
from datetime import datetime

msg_bp = Blueprint('message_center', __name__, url_prefix='/message_center')

ADMIN_PASSWORD = "MESSAGE_ADMIN_2025"

@msg_bp.route('/verify_admin', methods=['POST'])
def verify_admin():
    """Verifies password before allowing access to the panel."""
    password = request.json.get('password')
    if password == ADMIN_PASSWORD:
        session['is_admin'] = True
        return jsonify({"status": "success"})
    return jsonify({"status": "error"}), 401

@msg_bp.route('/panel')
def message_panel():
    if not session.get('is_admin'):
        return redirect(url_for('home.index'))
    return render_template('message_panel.html')

@msg_bp.route('/broadcast', methods=['POST'])
def broadcast():
    if not session.get('is_admin'): return jsonify({"status": "unauthorized"}), 403
    
    data = request.json
    msg_id = datetime.now().strftime("%Y%m%d%H%M%S")
    
    # Save the initial broadcast to MongoDB
    db.messages.insert_one({
        "msg_id": msg_id,
        "admin_message": data.get('message'),
        "offered_options": data.get('options', []),
        "timestamp": datetime.now(),
        "status": "active",
        "responses": []
    })

    socketio.emit('global_admin_message', {
        'message': data.get('message'),
        'options': data.get('options', []),
        'msg_id': msg_id
    }, namespace='/')
    
    return jsonify({"status": "success"})

@msg_bp.route('/acknowledge', methods=['POST'])
def acknowledge():
    data = request.json
    # Update the record in MongoDB with what the operator selected
    db.messages.update_one(
        {"msg_id": data.get('msg_id')},
        {"$set": {"status": "resolved", "selected_options": data.get('selections', []), "resolved_at": datetime.now()}}
    )
    socketio.emit('close_admin_message', namespace='/')
    return jsonify({"status": "acknowledged"})

@msg_bp.route('/history')
def message_history():
    # Only allow if the admin password was verified in this session
    if not session.get('is_admin'):
        return redirect(url_for('home.index'))
        
    history = list(db.messages.find().sort("timestamp", -1))
    return render_template('message_history.html', history=history)