from flask import Blueprint, render_template, request, jsonify
# We import socketio from your app initialization to send messages
from app import socketio 

sop_bp = Blueprint('sop', __name__, url_prefix='/sop')

# --- CONFIGURATION ---
ADMIN_PASSWORD = "admin" # Change this to your desired password

@sop_bp.route('/admin')
def admin_panel():
    """Renders the dashboard for supervisors to send messages."""
    return render_template('sop_admin.html')

@sop_bp.route('/broadcast', methods=['POST'])
def broadcast():
    """Receives message from Admin and sends to all clients."""
    data = request.json
    
    # 1. Verify Password
    if data.get('password') != ADMIN_PASSWORD:
        return jsonify({"status": "error", "message": "Invalid Password"}), 401
    
    # 2. Emit to ALL connected clients globally (namespace '/')
    socketio.emit('global_sop_alert', {
        'message': data.get('message'),
        'options': data.get('options', [])
    }, namespace='/')
    
    return jsonify({"status": "success"})

@sop_bp.route('/acknowledge', methods=['POST'])
def acknowledge():
    """Triggered by any client to clear screens everywhere."""
    socketio.emit('close_sop_alert', namespace='/')
    return jsonify({"status": "acknowledged"})