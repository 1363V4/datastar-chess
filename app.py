#!/usr/bin/env python
import asyncio
import logging
import os
import time
from datetime import timedelta
from random import choice
import chess
import redis.asyncio as redis
from datastar_py.responses import make_datastar_quart_response
from datastar_py.sse import ServerSentEventGenerator as SSE
from dotenv import load_dotenv
from faker import Faker
from quart import Quart, render_template, request, session, stream_with_context
from quart_rate_limiter import RateLimiter, rate_limit


# Configure logging
logging.basicConfig(
    filename='log/perso.log',
    level=logging.INFO,
    format='%(asctime)s - %(name)s - %(levelname)s - %(message)s'
)
logger = logging.getLogger(__name__)

# Init
load_dotenv()
app = Quart(__name__)
app.config['SECRET_KEY'] = os.getenv('QUART_SECRET_KEY')
limiter = RateLimiter(app)
fake = Faker()

# Redis connection pool
redis_pool = redis.ConnectionPool(host='localhost', port=6379, db=0, decode_responses=True)
redis_client = redis.Redis(connection_pool=redis_pool)

# Game
async def try_move(board_name, clicks):
    fen = await redis_client.lindex(f"board:{board_name}:fen", 0)
    board = chess.Board(fen)
    move = chess.Move(from_square=clicks[1], to_square=clicks[0])
    if board.piece_type_at(move.from_square) == chess.PAWN and move.to_square // 8 == 7:
      move.promotion = chess.QUEEN
    if move in board.legal_moves:
        board.push(move)
        await redis_client.lpush(f"board:{board_name}:fen", board.fen())
        game_result = get_game_result(board)
        if game_result:
            await redis_client.publish(board_name, f"Game over: {game_result}")
        return True
    return False

async def ai_move(board_name):
    fen = await redis_client.lindex(f"board:{board_name}:fen", 0)
    board = chess.Board(fen)
    
    if board.is_game_over():
        return
        
    legal_moves = list(board.legal_moves)
    move = choice(legal_moves)
    board.push(move)
    await asyncio.sleep(2)
    await redis_client.lpush(f"board:{board_name}:fen", board.fen())
    
    game_result = get_game_result(board)
    if game_result:
        await redis_client.publish(board_name, f"Game over: {game_result}")
    else:
        await redis_client.publish(board_name, "AI played")

def get_game_result(board):
    if board.is_checkmate():
        winner = "Black" if board.turn == chess.WHITE else "White"
        return f"{winner} wins by checkmate"
    elif board.is_stalemate():
        return "Draw by stalemate"
    elif board.is_insufficient_material():
        return "Draw by insufficient material"
    elif board.is_fifty_moves():
        return "Draw by fifty-move rule"
    elif board.is_repetition():
        return "Draw by repetition"
    elif board.is_game_over():
        return "Game over"
    return None

async def h_chessboard(fen):
    board = chess.Board(fen)
    
    legal_moves = list(board.legal_moves)
    possible = {n: [] for n in range(64)}
    for move in legal_moves:
        possible[move.to_square].append(move.from_square)
    
    html_parts = [
        '<div class="chessboard" data-signals-clicked__ifmissing=-1 data-on-click__outside="$clicked=-1">'
    ]

    for square in chess.SQUARES[::-1]:
        square_color = 'black' if (square // 8 + square) % 2 else 'white'
        square_attr = {
            'clicked': f'$clicked=={square}', 
            'possible': f'{possible.get(square, [])}.includes($clicked)',
            'size': '$clicked'
        }
        square_attr = str(square_attr).replace("'", "")
        
        square_html = f'<div class="square {square_color} gc gs" data-attr="{square_attr}" data-on-click="$clicked={square}">'
        
        piece = board.piece_at(square)
        if piece:
            color_suffix = 'l' if piece.color == chess.WHITE else 'd'
            piece_name = str(piece).lower()
            square_html += f'<img class="chess_img" src="/static/img/chess/pieces/{piece_name}{color_suffix}.svg">'
        
        square_html += "</div>"
        html_parts.append(square_html)

    html_parts.append("</div>")
    return ''.join(html_parts)

async def view(board_name, fen, player1, player2, opponent_turn, game_over_message=None):
    start_time = time.time()
    html_parts = [
        f'<main id="main" data-on-signals-change__viewTransition="@post(\'/chess\')">',
        f'<div class="room-name gc gt l">Room {board_name}</div>',
        f'<div id="player2" class="player-card">',
        f'<div class="player-bubble">{player2}</div>'
    ]
    
    if game_over_message:
        html_parts.append(f'<div class="gc game-over">{game_over_message}</div>')
    elif opponent_turn:
        html_parts.append('<div class="game-info gc gt s">Opponent thinking...</div>')
    
    html_parts.append('</div>')
    
    chessboard_html = await h_chessboard(fen)
    html_parts.append(chessboard_html)
    
    html_parts.extend([
        f'<div id="player1" class="player-card">',
        f'<div class="player-bubble">{player1}</div>',
        '</div>',
        '</main>'
    ])
    
    execution_time = time.time() - start_time
    logger.info(f"view executed in {execution_time:.4f} seconds")
    return ''.join(html_parts)

# App
@app.before_request
async def before_request():
    if not session.get('user_id'): 
        session['user_id'] = fake.name()

@app.get("/")
@rate_limit(10, timedelta(seconds=1))
async def index():
    return await render_template("index.html")

@app.get("/new_game")
@rate_limit(10, timedelta(seconds=1))
async def new_game():
    board_name = fake.color_name()
    session['board'] = board_name
    
    pipe = redis_client.pipeline()
    pipe.lpush(f"board:{board_name}:fen", chess.STARTING_FEN)
    pipe.lpush(f"user:{session['user_id']}:moves", -1)
    await pipe.execute()
    logger.info(f"New game created: {board_name} by {session['user_id']}")
    
    pubsub = redis_client.pubsub()
    await pubsub.subscribe(board_name)

    @stream_with_context
    async def event(generator):
        try:
            while True:
                message = await pubsub.get_message()
                if message:
                    start_time = time.time()
                    data = str(message.get('data', ""))
                    
                    game_over_message = None
                    opponent_turn = False
                    
                    if data.startswith("Game over"):
                        game_over_message = data
                        logger.info(f"Game over in {board_name}: {game_over_message}")
                    elif data == "Someone played":
                        opponent_turn = True
                    
                    fen = await redis_client.lindex(f"board:{board_name}:fen", 0)
                    html = await view(board_name, fen, session['user_id'], "AI", opponent_turn, game_over_message)
                    execution_time = time.time() - start_time
                    logger.info(f"responded to an event in {execution_time:.4f} seconds")
                    yield generator.merge_fragments(fragments=[html])
        except asyncio.CancelledError:
            logger.info(f"SSE connection for game {board_name} was cancelled")
        finally:
            await pubsub.unsubscribe(board_name)
    
    return await make_datastar_quart_response(event)

@app.post("/chess")
@rate_limit(10, timedelta(seconds=1))
async def chess_route():
    start_time = time.time()
    data = await request.json
    board_name = session['board']
    clicked = data.get('clicked')
    
    fen = await redis_client.lindex(f"board:{board_name}:fen", 0)
    board = chess.Board(fen)
    
    if board.is_game_over():
        return '', 204
        
    await redis_client.lpush(f"user:{session['user_id']}:moves", clicked)
    
    trait = fen.split()[1]
    if trait == "w":
        last_two_clicks = await redis_client.lrange(f"user:{session['user_id']}:moves", 0, 1)
        last_two_clicks = [int(click) for click in last_two_clicks]
        if await try_move(board_name, last_two_clicks):
            new_fen = await redis_client.lindex(f"board:{board_name}:fen", 0)
            new_board = chess.Board(new_fen)
            if not new_board.is_game_over():
                await redis_client.publish(board_name, "Someone played")
                asyncio.create_task(ai_move(board_name))
    
    execution_time = time.time() - start_time
    logger.info(f"chess_route executed in {execution_time:.4f} seconds")
    return '', 204

@app.before_serving
async def before_serving():
    logger.info("App starting up")

@app.after_serving
async def after_serving():
    logger.info("App shutting down")
    await redis_client.close()

if __name__ == "__main__":
    app.run(debug=True)
