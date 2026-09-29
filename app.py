python
import os
import jwt
import time
import json
from functools import wraps
from flask import Flask, request, jsonify
from flask_cors import CORS
from dotenv import load_dotenv

# =======================
#  LOAD ENVIRONMENT VARIABLES
# =======================

# .env फ़ाइल लोड करें (उसी डायरेक्टरी से जहाँ app.py है)
load_dotenv()

# Stringee क्रेडेंशियल्स - .env से पढ़ें
STRINGEE_API_KEY_SID = os.getenv("STRINGEE_API_KEY_SID")
STRINGEE_API_KEY_SECRET = os.getenv("STRINGEE_API_KEY_SECRET")
TOKEN_EXPIRY = int(os.getenv("TOKEN_EXPIRY", 604800))  # डिफॉल्ट 7 दिन

# सर्वर कॉन्फ़िगरेशन
HOST = os.getenv("HOST", "0.0.0.0")
PORT = int(os.getenv("PORT", 5000))

# चेक करें कि ज़रूरी क्रेडेंशियल्स मौजूद हैं
if not STRINGEE_API_KEY_SID or not STRINGEE_API_KEY_SECRET:
    raise ValueError("STRINGEE_API_KEY_SID and STRINGEE_API_KEY_SECRET must be set in .env file")

# =======================
#  INIT FLASK APP
# =======================

app = Flask(__name__)
CORS(app)  # Flutter ऐप को API कॉल करने की अनुमति

# =======================
#  HELPER FUNCTIONS
# =======================

def generate_stringee_token(user_id):
    """
    Stringee Client Authentication के लिए Access Token जनरेट करता है
    """
    # JWT Header - Stringee के दस्तावेज़ के अनुसार
    headers = {
        "typ": "JWT",
        "alg": "HS256",
        "cty": "stringee-api;v=1"
    }
    
    # JWT Payload - Stringee के दस्तावेज़ के अनुसार
    payload = {
        "jti": f"{STRINGEE_API_KEY_SID}_{int(time.time() * 1000)}",  # Unique ID
        "iss": STRINGEE_API_KEY_SID,
        "exp": int(time.time()) + TOKEN_EXPIRY,
        "userId": user_id
    }
    
    # JWT Token बनाएँ
    token = jwt.encode(payload, STRINGEE_API_KEY_SECRET, algorithm="HS256", headers=headers)
    return token


def verify_driver(driver_id, password):
    """
    ड्राइवर की पहचान Supabase से सत्यापित करता है
    (यहाँ आपको Supabase API कॉल लिखना है)
    """
    # TODO: Supabase से ड्राइवर की जानकारी लें और पासवर्ड मिलान करें
    # उदाहरण:
    # response = supabase.table('drivers').select('id, password').eq('id', driver_id).execute()
    # if response.data and response.data[0]['password'] == password:
    #     return True
    # return False
    
    # फिलहाल टेस्टिंग के लिए:
    return True


def verify_rider(rider_id):
    """
    राइडर की पहचान Supabase से सत्यापित करता है
    """
    # TODO: Supabase से राइडर की जानकारी लें और सत्यापन करें
    return True

# =======================
#  API ENDPOINTS
# =======================

@app.route('/health', methods=['GET'])
def health_check():
    """सर्वर की स्थिति जाँचने के लिए"""
    return jsonify({"status": "healthy", "message": "Server is running"}), 200


@app.route('/generate_token', methods=['POST'])
def generate_token():
    """
    Access Token जनरेट करने के लिए API
    Request Body: { "user_id": "driver_123", "user_type": "driver" }
    या { "user_id": "803e6c6e-...", "user_type": "rider" }
    """
    try:
        data = request.get_json()
        if not data:
            return jsonify({"error": "Invalid request body"}), 400
        
        user_id = data.get('user_id')
        user_type = data.get('user_type')  # 'driver' या 'rider'
        password = data.get('password')    # केवल ड्राइवर के लिए
        
        if not user_id or not user_type:
            return jsonify({"error": "user_id and user_type are required"}), 400
        
        # user_type के अनुसार सत्यापन
        if user_type == 'driver':
            if not password:
                return jsonify({"error": "Password required for driver"}), 400
            if not verify_driver(user_id, password):
                return jsonify({"error": "Invalid driver credentials"}), 401
        elif user_type == 'rider':
            if not verify_rider(user_id):
                return jsonify({"error": "Invalid rider credentials"}), 401
        else:
            return jsonify({"error": "Invalid user_type"}), 400
        
        # Token जनरेट करें
        token = generate_stringee_token(user_id)
        
        return jsonify({
            "success": True,
            "access_token": token,
            "expires_in": TOKEN_EXPIRY,
            "user_id": user_id,
            "message": "Token generated successfully"
        }), 200
        
    except jwt.PyJWTError as e:
        return jsonify({"error": f"Token generation failed: {str(e)}"}), 500
    except Exception as e:
        return jsonify({"error": f"Internal server error: {str(e)}"}), 500


@app.route('/verify_token', methods=['POST'])
def verify_token():
    """
    Token की वैधता जाँचने के लिए API
    Request Body: { "access_token": "eyJhbGc..." }
    """
    try:
        data = request.get_json()
        if not data or 'access_token' not in data:
            return jsonify({"error": "access_token is required"}), 400
        
        token = data['access_token']
        
        # Token को डिकोड करें और सत्यापन करें
        decoded = jwt.decode(
            token, 
            STRINGEE_API_KEY_SECRET, 
            algorithms=["HS256"],
            options={"verify_exp": True}
        )
        
        return jsonify({
            "success": True,
            "valid": True,
            "payload": decoded
        }), 200
        
    except jwt.ExpiredSignatureError:
        return jsonify({"success": False, "valid": False, "error": "Token has expired"}), 401
    except jwt.InvalidTokenError as e:
        return jsonify({"success": False, "valid": False, "error": f"Invalid token: {str(e)}"}), 401


@app.route('/revoke_token', methods=['POST'])
def revoke_token():
    """
    Token को निष्क्रिय करने के लिए API (लॉगआउट पर)
    Request Body: { "access_token": "eyJhbGc..." }
    """
    try:
        data = request.get_json()
        if not data or 'access_token' not in data:
            return jsonify({"error": "access_token is required"}), 400
        
        token = data['access_token']
        
        # TODO: Token को ब्लैकलिस्ट में डालें (उदाहरण के लिए Redis में)
        # यहाँ हम केवल सफलता का संदेश लौटा रहे हैं
        
        return jsonify({
            "success": True,
            "message": "Token revoked successfully"
        }), 200
        
    except Exception as e:
        return jsonify({"error": f"Failed to revoke token: {str(e)}"}), 500


# =======================
#  ERROR HANDLERS
# =======================

@app.errorhandler(404)
def not_found(error):
    return jsonify({"error": "Endpoint not found"}), 404

@app.errorhandler(500)
def internal_error(error):
    return jsonify({"error": "Internal server error"}), 500


# =======================
#  RUN SERVER
# =======================

if __name__ == '__main__':
    app.run(host=HOST, port=PORT, debug=True)