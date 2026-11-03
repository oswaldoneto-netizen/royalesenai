import os
import time
from gevent import monkey
monkey.patch_all()

from flask import Flask, send_from_directory, jsonify, request
from flask_socketio import SocketIO, emit, join_room, leave_room

app = Flask(__name__, static_folder='static')
app.config['SECRET_KEY'] = 'royale-senai-secret'

socketio = SocketIO(app, cors_allowed_origins="*", async_mode='gevent')

queue = []
rooms = {}
MAX_PLAYERS = 12

@app.route('/')
def index():
    return send_from_directory('static', 'index.html')

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
    nick = data.get('nick', 'Jogador').strip()
    if not nick:
        nick = 'Jogador'
        
    return jsonify({
        'token': f'token_{nick}',
        'nick': nick,
        'display': nick,
        'save': {'xp': 0, 'coins': 500, 'cown': [0], 'wown': [0], 'ec': 0, 'ew': 0}
    })

@app.route('/api/register', methods=['POST', 'OPTIONS'])
def register():
    return login()

@app.route('/api/me', methods=['GET', 'OPTIONS'])
def me():
    if request.method == 'OPTIONS':
        return jsonify({'status': 'ok'}), 200
    return jsonify({
        'token': 'token_demo',
        'nick': 'Jogador',
        'display': 'Jogador',
        'save': {'xp': 0, 'coins': 500, 'cown': [0], 'wown': [0], 'ec': 0, 'ew': 0}
    })

@app.route('/api/online', methods=['GET', 'POST', 'OPTIONS'])
def online():
    return jsonify({'online': len(queue) + sum(len(r['players']) for r in rooms.values())})

@app.route('/api/ping', methods=['POST', 'OPTIONS'])
def ping():
    return jsonify({'online': 1})

@app.route('/api/save', methods=['POST', 'OPTIONS'])
def save():
    return jsonify({'status': 'ok'})

@socketio.on('connect')
def handle_connect():
    pass

@socketio.on('disconnect')
def handle_disconnect():
    sid = request.sid
    for p in queue:
        if p['sid'] == sid:
            queue.remove(p)
            break

@socketio.on('join_match')
def handle_join_match(data):
    sid = request.sid
    player_info = {
        'sid': sid,
        'nick': data.get('nick', 'Jogador'),
        'skin': data.get('skin', 0)
    }
    
    if not any(p['sid'] == sid for p in queue):
        queue.append(player_info)
    
    emit('lobby', {'n': len(queue), 'left': 3}, broadcast=True)
    
    if len(queue) >= MAX_PLAYERS:
        start_room()

def start_room():
    if not queue:
        return
        
    room_id = f"room_{int(time.time())}"
    current_players = queue[:MAX_PLAYERS]
    del queue[:MAX_PLAYERS]
    
    real_count = len(current_players)
    bot_count = MAX_PLAYERS - real_count
    
    rooms[room_id] = {
        'players': current_players,
        'bots': bot_count
    }
    
    for p in current_players:
        join_room(room_id, sid=p['sid'])
        
    socketio.emit('start_game', {
        'room': room_id,
        'real_players': [p['nick'] for p in current_players],
        'bots_count': bot_count
    }, room=room_id)

if __name__ == '__main__':
    port = int(os.environ.get('PORT', 5000))
    socketio.run(app, host='0.0.0.0', port=port)
    
