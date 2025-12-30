from flask import Blueprint, render_template

# Create the blueprint
home_bp = Blueprint('home', __name__)

@home_bp.route('/')
def index():
    """
    Render the Home Page.
    """
    try:
        return render_template('index.html')
    except Exception as e:
        # Basic error handling
        return f"An error occurred loading the home page: {str(e)}", 500

@home_bp.route('/parts')
def parts_management():
    """
    Placeholder for Parts Management.
    """
    return render_template('base.html') # Just rendering base for now to show Nav