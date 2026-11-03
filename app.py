import os
import time
from gevent import monkey
monkey.patch_all()

from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'royale-senai-secret'

# SocketIO configurado para aceitar requisições de qualquer origem
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

queue = []
rooms = {}

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# Libera requisições CORS para o navegador do celular não bloquear o envio
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization, X-Token'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
    data = request.get_json(silent=True) or {}
    nick = data.get('nick', 'Jogador').strip() or 'Jogador'
    return jsonify({
        'token': f'token_{nick}',
        'nick': nick,
        'display': nick,
        'save': {'xp': 0, 'coins': 500, 'cown': [0], 'wown': [0], 'ec': 0, 'ew': 0}
    })

@app.route('/api/register', methods=['POST', 'OPTIONS'])
def register():
    return login()

@app.route('/api/me', methods=['GET', 'POST', 'OPTIONS'])
def me():
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
    token = request.headers.get('X-Token', 'token_Jogador')
    nick = token.replace('token_', '') if 'token_' in token else 'Jogador'
    return jsonify({
        'token': token,
        'nick': nick,
        'display': nick,
        'save': {'xp': 0, 'coins': 500, 'cown': [0], 'wown': [0], 'ec': 0, 'ew': 0}
    })

@app.route('/api/online', methods=['GET', 'POST', 'OPTIONS'])
def online():
    return jsonify({'online': len(queue)})

@app.route('/api/ping', methods=['POST', 'OPTIONS'])
def ping():
    return jsonify({'online': len(queue)})

@app.route('/api/save', methods=['POST', 'OPTIONS'])
def save():
    return jsonify({'status': 'ok'})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
    
