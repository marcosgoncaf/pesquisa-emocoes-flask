import os
import json
import random
import string
import base64
import numpy as np
import cv2
import gspread
from flask import Flask, render_template, request, jsonify
from deepface import DeepFace
from datetime import datetime

app = Flask(__name__)

# =============================================================================
# --- CONFIGURAÇÃO E CONEXÃO COM GOOGLE SHEETS ---
# =============================================================================
try:
    # Tenta conectar usando arquivo local (Desenvolvimento no PC)
    if os.path.exists("credentials.json"):
        sa = gspread.service_account("credentials.json")
    else:
        # Se não achar o arquivo, tenta usar a Variável de Ambiente (Produção no Render)
        # O Render guarda o JSON como string na variável GOOGLE_CREDENTIALS
        creds_json = os.environ.get("GOOGLE_CREDENTIALS")
        if creds_json:
            creds_dict = json.loads(creds_json)
            sa = gspread.service_account_from_dict(creds_dict)
        else:
            raise Exception("Nenhuma credencial encontrada (Arquivo ou Variável de Ambiente).")

    # Abre a planilha pelo nome exato
    sh = sa.open("Resultados Pesquisa Emoções")

except Exception as e:
    print(f"❌ ERRO CRÍTICO DE CONEXÃO: {e}")
    # Não paramos o app aqui para permitir ver o erro no log do servidor,
    # mas as funções de banco de dados vão falhar.

# =============================================================================
# --- FUNÇÕES AUXILIARES ---
# =============================================================================
def base64_to_image(base64_string):
    """Converte a imagem que vem do Javascript (Base64) para formato OpenCV"""
    if ',' in base64_string:
        base64_string = base64_string.split(',')[1]
    nparr = np.frombuffer(base64.b64decode(base64_string), np.uint8)
    img = cv2.imdecode(nparr, cv2.IMREAD_COLOR)
    return img

# =============================================================================
# --- ROTAS (PÁGINAS DO SITE) ---
# =============================================================================

@app.route('/')
def home():
    """Carrega a página do PARTICIPANTE"""
    # O ID do estudo vem pela URL (ex: site.com/?study_id=xyz)
    study_id = request.args.get('study_id')
    
    # Se tiver ID, buscamos a configuração na aba "Estudos"
    study_config = None
    if study_id:
        try:
            ws = sh.worksheet("Estudos")
            # Procura o ID na Coluna 1 (A)
            cell = ws.find(study_id, in_column=1)
            if cell:
                # A configuração JSON está na Coluna 2 (B), na mesma linha
                config_json = ws.cell(cell.row, 2).value
                study_config = json.loads(config_json)
        except Exception as e:
            print(f"Erro ao buscar estudo: {e}")

    # Passamos a configuração para o HTML (ou None se não achar)
    return render_template('index.html', study_id=study_id, study_config=study_config)

@app.route('/admin')
def admin_panel():
    """Carrega a página do PESQUISADOR (Admin)"""
    # Renderiza o arquivo templates/admin.html
    return render_template('admin.html')

# =============================================================================
# --- API (O CÉREBRO POR TRÁS) ---
# =============================================================================

@app.route('/create_study', methods=['POST'])
def create_study():
    """Cria um novo estudo e salva no Google Sheets"""
    data = request.json
    try:
        # 1. Gera ID único de 8 caracteres
        study_id = ''.join(random.choices(string.ascii_lowercase + string.digits, k=8))
        
        # 2. Monta o pacote de dados
        config = {
            "study_name": data.get('study_name'),
            "welcome_message": data.get('welcome_message'),
            "items": data.get('items'),
            "exposure_time": int(data.get('exposure_time', 5000)),
            "created_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        }
        
        # 3. Garante que a aba "Estudos" existe
        try:
            ws = sh.worksheet("Estudos")
        except:
            ws = sh.add_worksheet(title="Estudos", rows=100, cols=5)
            ws.append_row(["ID do Estudo", "Configuração JSON"]) # Cabeçalho
            
        # 4. Salva na planilha
        ws.append_row([study_id, json.dumps(config)])
        
        # 5. Gera o link final
        base_url = request.host_url.rstrip('/')
        full_link = f"{base_url}/?study_id={study_id}"
        
        return jsonify({'status': 'success', 'link': full_link, 'id': study_id})
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

@app.route('/check_face', methods=['POST'])
def check_face():
    """Verifica se existe um rosto na imagem enviada"""
    data = request.json
    try:
        img = base64_to_image(data['image'])
        
        # Usa o classificador padrão do OpenCV
        cascade_path = cv2.data.haarcascades + 'haarcascade_frontalface_default.xml'
        face_cascade = cv2.CascadeClassifier(cascade_path)
        
        gray = cv2.cvtColor(img, cv2.COLOR_BGR2GRAY)
        faces = face_cascade.detectMultiScale(gray, 1.1, 4)
        
        return jsonify({'face_detected': len(faces) > 0})
    except Exception as e:
        print(f"Erro check_face: {e}")
        return jsonify({'error': str(e)}), 500

@app.route('/analyze_emotion', methods=['POST'])
def analyze_emotion_route():
    """Analisa a emoção usando DeepFace"""
    data = request.json
    try:
        img = base64_to_image(data['image'])
        
        # DeepFace retorna uma lista de resultados
        analysis = DeepFace.analyze(img_path=img, actions=['emotion'], enforce_detection=False)
        
        dominant = "não_detectado"
        if isinstance(analysis, list) and len(analysis) > 0:
            dominant = analysis[0]['dominant_emotion']
        elif isinstance(analysis, dict):
            dominant = analysis['dominant_emotion']
            
        return jsonify({'emotion': dominant})
    except Exception as e:
        print(f"Erro DeepFace: {e}")
        return jsonify({'emotion': 'erro'})

@app.route('/save_data', methods=['POST'])
def save_data():
    """Salva as respostas dos participantes"""
    data = request.json
    try:
        pid = data.get('participant_id')
        sid = data.get('study_id')
        results = data.get('results', [])
        
        rows = []
        for item in results:
            # Garante que a lista de emoções não quebre se estiver vazia
            emotions_list = item.get('emotions_list', [])
            first_emotion = emotions_list[0] if len(emotions_list) > 0 else "N/A"
            
            rows.append([
                pid,
                sid,
                item.get('stimulus'),
                datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                first_emotion, # Emoção dominante capturada
                item.get('liking'),
                ", ".join(map(str, emotions_list)), # Todas as emoções detectadas
                item.get('word')
            ])
            
        # Garante que a aba "Resultados" existe
        try:
            ws = sh.worksheet("Resultados")
        except:
            ws = sh.add_worksheet(title="Resultados", rows=1000, cols=10)
            ws.append_row(["Participante", "ID Estudo", "Estímulo", "Data/Hora", "Emoção Princ.", "Nota", "Emoções Detalhadas", "Palavra"])
            
        ws.append_rows(rows)
        return jsonify({'status': 'success'})
        
    except Exception as e:
        return jsonify({'status': 'error', 'message': str(e)}), 500

if __name__ == '__main__':
    # Roda o servidor
    app.run(debug=True, port=5000)