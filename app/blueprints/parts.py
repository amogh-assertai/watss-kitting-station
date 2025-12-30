from flask import Blueprint, render_template, request, jsonify, flash, redirect, url_for
from app.db import get_db
from bson.objectid import ObjectId
from datetime import datetime

parts_bp = Blueprint('parts', __name__, url_prefix='/parts')

@parts_bp.route('/')
def list_kits():
    """Lists all created kits."""
    try:
        db = get_db()
        # Fetch all kits, sorted by newest first
        kits = list(db.kits.find().sort("created_at", -1))
        return render_template('parts_list.html', kits=kits)
    except Exception as e:
        flash(f"Error loading kits: {str(e)}", "danger")
        return render_template('parts_list.html', kits=[])

@parts_bp.route('/create', methods=['GET'])
def create_kit_form():
    """Renders the form to create a new kit."""
    return render_template('parts_form.html', kit=None)

@parts_bp.route('/edit/<kit_id>', methods=['GET'])
def edit_kit_form(kit_id):
    """Renders the form pre-filled with existing kit data."""
    try:
        db = get_db()
        kit = db.kits.find_one({"_id": ObjectId(kit_id)})
        if not kit:
            flash("Kit not found.", "warning")
            return redirect(url_for('parts.list_kits'))
        
        # Convert ObjectId to string for the template
        kit['id'] = str(kit['_id'])
        return render_template('parts_form.html', kit=kit)
    except Exception as e:
        flash(f"Error loading kit: {str(e)}", "danger")
        return redirect(url_for('parts.list_kits'))

@parts_bp.route('/save', methods=['POST'])
def save_kit():
    """API Endpoint to Save (Insert or Update) a Kit."""
    try:
        data = request.json
        db = get_db()

        # Validate basic fields
        if not data.get('kit_name') or not data.get('edp_number'):
            return jsonify({'status': 'error', 'message': 'Kit Name and EDP Number are required.'}), 400

        kit_doc = {
            "kit_name": data['kit_name'],
            "edp_number": data['edp_number'],
            "parts": data.get('parts', []), # List of part objects
            "updated_at": datetime.utcnow()
        }

        if data.get('kit_id'):
            # Update existing kit
            db.kits.update_one(
                {"_id": ObjectId(data['kit_id'])},
                {"$set": kit_doc}
            )
            message = "Kit updated successfully!"
        else:
            # Create new kit
            kit_doc["created_at"] = datetime.utcnow()
            db.kits.insert_one(kit_doc)
            message = "Kit created successfully!"

        return jsonify({'status': 'success', 'message': message, 'redirect': url_for('parts.list_kits')})

    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@parts_bp.route('/delete/<kit_id>', methods=['POST'])
def delete_kit(kit_id):
    """Deletes a kit."""
    try:
        db = get_db()
        db.kits.delete_one({"_id": ObjectId(kit_id)})
        flash("Kit deleted.", "success")
    except Exception as e:
        flash(f"Error deleting kit: {str(e)}", "danger")
    
    return redirect(url_for('parts.list_kits'))