
from flask import Flask, jsonify

app = Flask(__name__)

@app.route("/")
def home():
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
    app.run(debug=True)
