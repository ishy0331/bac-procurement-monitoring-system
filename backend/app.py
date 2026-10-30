
from pathlib import Path
from flask import Flask, jsonify, send_from_directory
from db import get_db_connection

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_FILE = "proctest19.html"

app = Flask(__name__, static_folder=None)


# Serve the existing staff frontend
@app.route("/")
def index():
    return send_from_directory(PROJECT_DIR, FRONTEND_FILE)


# API root
@app.route("/api")
def api_root():
    return jsonify({
        "message": "Procurement Monitoring System Backend is running!"
    })


# Check if Flask is working
@app.route("/api/health")
def health():
    return jsonify({
        "status": "success",
        "message": "Flask API is working!"
    })


# Check database connection and available tables
@app.route("/api/db-test")
def db_test():
    try:
        connection = get_db_connection()
        cursor = connection.cursor()

        cursor.execute("SHOW TABLES")
        tables = [row[0] for row in cursor.fetchall()]

        cursor.close()
        connection.close()

        return jsonify({
            "status": "success",
            "database": "procurement_db_test",
            "table_count": len(tables),
            "tables": tables
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


# Retrieve procurement records
@app.route("/api/procurements")
def get_procurements():
    try:
        connection = get_db_connection()
        cursor = connection.cursor(dictionary=True)

        cursor.execute("""
            SELECT
                procurement_id,
                reference_no,
                title,
                abc,
                status,
                data_source
            FROM procurements
            ORDER BY procurement_id DESC
        """)

        procurements = cursor.fetchall()

        cursor.close()
        connection.close()

        return jsonify({
            "status": "success",
            "count": len(procurements),
            "procurements": procurements
        })

    except Exception as e:
        return jsonify({
            "status": "error",
            "message": str(e)
        }), 500


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)
