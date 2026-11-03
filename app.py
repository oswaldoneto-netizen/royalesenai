import os
import time
from gevent import monkey
monkey.patch_all()

from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'royale-senai-secret'

# SocketIO com CORS liberado para qualquer origem
socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

queue = []
rooms = {}
MAX_PLAYERS = 12

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

# Tratamento global de CORS para as rotas HTTP da API
@app.after_request
def add_cors_headers(response):
    response.headers['Access-Control-Allow-Origin'] = '*'
    response.headers['Access-Control-Allow-Headers'] = 'Content-Type, Authorization'
    response.headers['Access-Control-Allow-Methods'] = 'GET, POST, OPTIONS'
    return response

@app.route('/api/login', methods=['POST', 'OPTIONS'])
def login():
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
        
    data = request.get_json(silent=True) or {}
    nick = data.get('nick', 'Jogador')
    return jsonify({
        'token': f'token_{nick}',
        'nick': nick,
        'display': nick,
        'save': {'xp': 0, 'coins': 500, 'cown': [0], 'wown': [0], 'ec': 0, 'ew': 0}
    })

@app.route('/api/register', methods=['POST', 'OPTIONS'])
def register():
    return login()

@app.route('/api/online', methods=['GET', 'POST'])
def online():
    return jsonify({'online': len(queue) + sum(len(r['players']) for r in rooms.values())})

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
    
