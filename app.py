import os
import json
import base64
import numpy as np
import cv2
import gspread
from flask import Flask, render_template, request, jsonify
from deepface import DeepFace
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURAÇÃO ---
# Carrega credenciais. Em produção (Render), usaremos variáveis de ambiente,
# mas para testar local, ele busca o arquivo.
try:
    if os.path.exists("credentials.json"):
        sa = gspread.service_account("credentials.json")
    else:
        # Fallback para ler de variável de ambiente (para deploy futuro)
        creds_dict = json.loads(os.environ.get("GOOGLE_CREDENTIALS"))
        sa = gspread.service_account_from_dict(creds_dict)
    
    sh = sa.open("Resultados Pesquisa Emoções") # Mude para o nome EXATO da sua planilha
except Exception as e:
    print(f"Erro ao conectar Google Sheets: {e}")

# --- FUNÇÕES AUXILIARES ---
def base64_to_image(base64_string):
    """Converte a string base64 do Javascript para imagem OpenCV"""
    encoded_data = base64_string.split(',')[1]
    nparr = np.frombuffer(base64.b64decode(encoded_data), np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    return img

# --- ROTAS (Endereços do Site) ---

@app.route('/')
def home():
    """Carrega a página inicial"""
    # Aqui você pode capturar o ID da URL igual fazia no Streamlit
    study_id = request.args.get('study_id', 'default') 
    
    # Lógica para buscar config no Sheets (Simplificada)
    # No futuro, você busca as configurações aqui e passa para o HTML
    return render_template('index.html', study_id=study_id)

@app.route('/check_face', methods=['POST'])
def check_face():
    """Verifica se tem um rosto na câmera (fase de teste)"""
    data = request.json
    try:
        img = base64_to_image(data['image'])
        
        # Detecção rápida usando OpenCV puro para não pesar
        face_cascade = cv2.CascadeClassifier(cv2.data.haarcascades + 'haarcascade_frontalface_default.xml')
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e:
        return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    """Analisa emoção de um frame específico"""
    data = request.json
    try:
        img = base64_to_image(data['image'])
        # DeepFace analysis
        analysis = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False)
        dominant = analysis[0]['dominant_emotion'] if isinstance(analysis, list) else "não_detectado"
        return jsonify({'emotion': dominant})
    except Exception as e:
        print(f"Erro DeepFace: {e}")
        return jsonify({'emotion': 'erro'})

@app.route('/save_data', methods=['POST'])
def save_data():
    """Salva os resultados finais no Google Sheets"""
    data = request.json
    # data espera receber: {participant_id, study_id, results: [...]}
    
    try:
        rows = []
        pid = data.get('participant_id')
        sid = data.get('study_id')
        
        for item in data.get('results', []):
            # Formata a linha igual você fazia no Streamlit
            rows.append([
                pid, 
                sid, 
                item.get('stimulus'), 
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                item.get('emotions', ["N/A"])[0], # Pega a primeira emoção analisada
                item.get('liking'),
                str(item.get('emotions_list')),
                item.get('word')
            ])
            
        worksheet = sh.worksheet("Resultados") # Garanta que a aba existe
        worksheet.append_rows(rows)
        return jsonify({'status': 'success'})
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    # Configuração para rodar localmente
    app.run(debug=True, port=5000)