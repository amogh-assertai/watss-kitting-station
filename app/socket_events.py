from flask_socketio import SocketIO, emit, join_room, leave_room
from app.db import get_db
from datetime import datetime

socketio = SocketIO(cors_allowed_origins="*")

@socketio.on('connect')
def handle_connect():
    print("⚡ Client Connected")

@socketio.on('disconnect')
def handle_disconnect():
    print("❌ Client Disconnected")

@socketio.on('join_table')
def on_join(data):
    """
    UI or AI joins a specific Table's room.
    data = {'table_id': '1'}
    """
    room = f"table_{data['table_id']}"
    join_room(room)
    print(f"👥 Client joined room: {room}")
    emit('status_update', {'message': f'Joined {room}'}, to=room)

@socketio.on('ai_update')
def handle_ai_update(data):
    """
    AI sends an update (e.g., 'part_placed').
    data = {
        'table_id': '1', 
        'step_index': 2, 
        'status': 'success', 
        'error': None
    }
    """
    try:
        table_id = data.get('table_id')
        room = f"table_{table_id}"
        
        # 1. Save to MongoDB (The "Integrated" Advantage!)
        # We can directly access the DB here.
        # Note: We need a manual DB connection here because this event 
        # might happen outside a web request context, but Flask-SocketIO handles contexts well.
        # For simplicity in this snippet, we skip complex DB writes, 
        # but you WOULD put your 'db.activities.update(...)' logic here.
        
        timestamp = datetime.now().strftime("%H:%M:%S")
        print(f"🤖 AI Update for {room}: {data}")

        # 2. Broadcast to UI (in real-time)
        emit('ui_update', {
            'type': 'step_completed',
            'data': data,
            'time': timestamp
        }, to=room)
        
    except Exception as e:
        print(f"Error processing AI update: {e}")

@socketio.on('ui_command')
def handle_ui_command(data):
    """
    UI sends command to AI (e.g., 'stop_process', 'retry').
    """
    table_id = data.get('table_id')
    room = f"table_{table_id}"
    
    print(f"🖥️ UI Command for {room}: {data}")
    
    # Broadcast to everyone in room (specifically the AI listener)
    emit('ai_command', data, to=room)
    
    
@socketio.on('create_activity_signal')
def handle_creation_signal(data):
    """
    Relays the 'creating-new-activity' signal from UI to AI.
    """
    table_id = data.get('tableId')
    room = f"table_{table_id}"
    print(f"📡 Relaying creation signal to {room}: {data}")
    
    # Broadcast to the room so the AI client (listening on this event) gets it
    emit('create_activity_signal', data, to=room, include_self=False)

@socketio.on('ai_handshake_response')
def handle_ai_handshake(data):
    """
    Relays the 'starting-ai-application' confirmation from AI to UI.
    """
    table_id = data.get('tableId')
    room = f"table_{table_id}"
    print(f"🤝 AI Handshake received from {room}: {data}")
    
    # Send back to UI
    emit('ai_handshake_response', data, to=room)
    
    
# Add these to app/socket_events.py

# --- CAMERA 1 ---
@socketio.on('capture_cam1_signal')
def relay_cam1_capture(data):
    room = f"table_{data.get('tableId')}"
    emit('capture_cam1_signal', data, to=room, include_self=False)

@socketio.on('sending_cam1_ack')
def relay_cam1_ack(data):
    room = f"table_{data.get('tableId')}"
    emit('sending_cam1_ack', data, to=room)

@socketio.on('cam1_result')
def relay_cam1_result(data):
    room = f"table_{data.get('tableId')}"
    emit('cam1_result', data, to=room)

# --- CAMERA 2 ---
@socketio.on('capture_cam2_signal')
def relay_cam2_capture(data):
    room = f"table_{data.get('tableId')}"
    emit('capture_cam2_signal', data, to=room, include_self=False)

@socketio.on('sending_cam2_ack')
def relay_cam2_ack(data):
    room = f"table_{data.get('tableId')}"
    emit('sending_cam2_ack', data, to=room)

@socketio.on('cam2_result')
def relay_cam2_result(data):
    room = f"table_{data.get('tableId')}"
    emit('cam2_result', data, to=room)