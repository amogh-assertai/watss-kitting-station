from pymongo import MongoClient
from pymongo.errors import ConnectionFailure
from flask import current_app, g

def get_db():
    """
    Opens a new database connection if there is none yet for the
    current application context.
    """
    if 'db' not in g:
        try:
            # Connect to MongoDB using the URI from config
            client = MongoClient(current_app.config['MONGO_URI'], serverSelectionTimeoutMS=5000)
            # Check connection
            client.admin.command('ping')
            g.db = client[current_app.config['DB_NAME']]
        except ConnectionFailure as e:
            current_app.logger.error(f"MongoDB Connection Failed: {e}")
            raise e
    return g.db

def close_db(e=None):
    """Closes the database connection at the end of the request."""
    db = g.pop('db', None)
    if db is not None:
        # In PyMongo, we strictly close the client, not the database object
        db.client.close()

def init_app(app):
    """Register database functions with the Flask app."""
    app.teardown_appcontext(close_db)