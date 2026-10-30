from pathlib import Path

from flask import Flask, jsonify, send_from_directory

BACKEND_DIR = Path(__file__).resolve().parent
PROJECT_DIR = BACKEND_DIR.parent
FRONTEND_FILE = "proctest19.html"

app = Flask(__name__, static_folder=None)


@app.route("/")
def index():
    return send_from_directory(PROJECT_DIR, FRONTEND_FILE)


@app.route("/api")
def api_root():
    return jsonify({
        "message": "Procurement Monitoring System Backend is running!"
    })


@app.route("/api/health")
def health():
    return jsonify({
        "status": "success",
        "message": "Flask API is working!"
    })


if __name__ == "__main__":
    app.run(host="127.0.0.1", port=5000, debug=True)