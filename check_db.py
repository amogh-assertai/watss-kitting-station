from app import create_app
from app.db import get_db

app = create_app()

with app.app_context():
    db = get_db()
    print("\n" + "="*40)
    print("🕵️  DATABASE INSPECTOR")
    print("="*40)
    
    # 1. List all Collections
    print(f"📂 Collections found: {db.list_collection_names()}")
    
    # 2. Dump everything in 'parts'
    parts = list(db.kits.find())
    print(f"📦 Total Parts/Kits found: {len(parts)}")
    
    if len(parts) == 0:
        print("⚠️  The 'parts' collection is EMPTY!")
    else:
        for p in parts:
            print("-" * 20)
            # Print specific fields to check spelling
            print(f"ID: {p.get('_id')}")
            print(f"Fields: {list(p.keys())}") # Shows us the actual field names
            print(f"Values: {p}")
    
    print("="*40 + "\n")