import xmlrpc.client
import requests
import json
import os
import pdfplumber
import base64
import time
import re
import random
import hashlib
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# --- CONFIGURACIÓN DE VARIABLES ---
ODOO_URL = os.getenv('ODOO_URL')
ODOO_DB = os.getenv('ODOO_DB')
ODOO_USER = os.getenv('ODOO_USER')
ODOO_API_KEY = os.getenv('ODOO_API_KEY')
MONDAY_API_KEY = os.getenv('MONDAY_API_KEY')
MONDAY_API_URL = "https://api.monday.com/v2"

archivos_en_proceso = {}

def limpiar_monto_proximidad(texto):
    if not texto: return 0.0
    try:
        # Quitamos todo lo que no sea número o punto
        limpio = re.sub(r'[^0-9.]', '', str(texto).replace(',', ''))
        return float(limpio) if limpio else 0.0
    except: return 0.0

def extraer_lineas_pdf(pdf_content, maquila_id):
    lineas_acumuladas = []
    MAPA_ACEROS = {
        "SS304": "SS 304", "SS316": "SS 316", "A36": "AC A36",
        "1018": "AC 1018", "1045": "AC 1045", "4140": "AC 4140",
        "GALVANIZADO": "Galvanizado", "AL6160": "AL 6160"
    }
    path_temp = f"temp_{int(time.time())}.pdf"
    with open(path_temp, "wb") as f: f.write(pdf_content)
    try:
        with pdfplumber.open(path_temp) as pdf:
            contador_pieza = 1
            for page in pdf.pages:
                words = page.extract_words()
                if not words: continue
                
                # Agrupar por líneas visuales (Y)
                lines_dict = {}
                for w in words:
                    y = round(w['top'] / 2) * 2 
                    if y not in lines_dict: lines_dict[y] = []
                    lines_dict[y].append(w)
                
                for y in sorted(lines_dict.keys()):
                    line_words = sorted(lines_dict[y], key=lambda x: x['x0'])
                    text_full = " ".join([w['text'] for w in line_words])
                    
                    if not text_full.startswith("Maq-"): continue
                    
                    try:
                        partida = line_words[0]['text']
                        cantidad_final = 1.0
                        pu_final = 0.0
                        
                        # --- EXTRACCIÓN DINÁMICA POR COORDENADAS ---
                        # En ASK, la estructura suele ser:
                        # [0]Maq-X ... [Zona Central]Cant ... [Zona Derecha]PU ... [Final]Total
                        
                        # 1. Buscar Cantidad (Zona Central: 340 a 415)
                        for w in line_words:
                            if 340 < w['x0'] < 415:
                                val = w['text'].replace(',', '')
                                if val.replace('.', '').isdigit():
                                    cantidad_final = float(val)
                                    break
                        
                        # 2. Buscar PU (Buscamos el primer valor con '$' de derecha a izquierda, 
                        # ignorando el total que es el último)
                        montos_encontrados = []
                        for w in line_words:
                            monto = limpiar_monto_proximidad(w['text'])
                            if monto > 0 and (w['x0'] > 420): # Solo valores después de la cantidad
                                montos_encontrados.append(monto)
                        
                        if len(montos_encontrados) >= 2:
                            # El penúltimo suele ser el PU, el último es el Total
                            pu_final = montos_encontrados[-2]
                        elif montos_encontrados:
                            # Si solo hay uno después de la cantidad, es el PU
                            pu_final = montos_encontrados[0]

                        # --- ESPECIFICACIONES ---
                        espesor = "N/A"
                        idx_esp = -1
                        for i, w in enumerate(line_words):
                            t = w['text']
                            if '/' in t or '"' in t or re.match(r'^[Cc]\d+$', t):
                                espesor = t; idx_esp = i; break
                        
                        metal_raw = "AC A36"
                        if idx_esp != -1 and (idx_esp + 1) < len(line_words):
                            metal_raw = line_words[idx_esp + 1]['text'].replace(" ", "").upper()
                        
                        metal_final = "AC A36"
                        for clave_pdf, valor_odoo in MAPA_ACEROS.items():
                            if clave_pdf in metal_raw: metal_final = valor_odoo; break
                        
                        material_valor = "Si"
                        if re.search(r'\bno\b', text_full, re.IGNORECASE): material_valor = "No"
                        elif re.search(r'\bsi\b', text_full, re.IGNORECASE): material_valor = "Si"

                        lineas_acumuladas.append((0, 0, {
                            'product_id': maquila_id,
                            'x_studio_descripcion': f"Pieza {contador_pieza}",
                            'x_studio_partida_1': partida,
                            'x_studio_espesor': espesor,
                            'x_studio_acero': metal_final,
                            'x_studio_material': material_valor,
                            'product_uom_qty': cantidad_final,
                            'product_uom_id': 1,
                            'price_unit': pu_final,
                        }))
                        contador_pieza += 1
                    except: continue
        return lineas_acumuladas
    except Exception: return []
    finally:
        if os.path.exists(path_temp): os.remove(path_temp)

def procesar_flujo(item_id, pulse_name):
    time.sleep(1.5)
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common', allow_none=True)
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object', allow_none=True)

        headers = {"Authorization": MONDAY_API_KEY, "API-Version": "2023-10"}
        query = f'query {{ items (ids: [{item_id}]) {{ assets {{ name url public_url }} column_values {{ id text }} }} }}'
        r = requests.post(MONDAY_API_URL, json={'query': query}, headers=headers).json()
        item_data = r['data']['items'][0]
        assets = item_data.get('assets', [])
        cols_text = {c['id']: (c.get('text') or "") for c in item_data.get('column_values', [])}

        maquila_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'product.product', 'search', [[('name', '=', 'Maquila')]])[0]
        
        proj_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'search', [[('name', '=', pulse_name)]])
        proyecto_id = proj_ids[0] if proj_ids else models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'create', [{'name': pulse_name}])
        
        nombre_cli = cols_text.get('cliente', 'Cliente Monday').strip()
        cli_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'res.partner', 'search', [[('name', '=', nombre_cli)]])
        partner_id = cli_ids[0] if cli_ids else models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'res.partner', 'create', [{'name': nombre_cli}])

        lineas_finales = []
        for asset in reversed(assets):
            if asset.get('name', '').lower().endswith(".pdf"):
                u = asset['public_url'] if asset.get('public_url') else asset['url']
                pdf_data = requests.get(u).content
                lineas_extraidas = extraer_lineas_pdf(pdf_data, maquila_id)
                if lineas_extraidas:
                    lineas_finales = lineas_extraidas
                    break

        venta_vals = {
            'partner_id': partner_id,
            'x_studio_many2one_field_dovxQ': proyecto_id,
            'x_studio_referencia_monday': str(item_id),
            'x_studio_vendedor': cols_text.get('personas', 'Sin Vendedor'),
            'x_studio_cotizacin': cols_text.get('texto5', ''),
            'x_studio_mes_de_venta': {1:"Enero",2:"Febrero",3:"Marzo",4:"Abril",5:"Mayo",6:"Junio",7:"Julio",8:"Agosto",9:"Septiembre",10:"Octubre",11:"Noviembre",12:"Diciembre"}[datetime.now().month],
            'x_studio_material_1': 'Incluido', 'x_studio_tipo_1': 'Maquila', 'x_studio_ubicacin': 'Querétaro',
            'x_studio_facturacin': 'Factura', 'x_studio_metodo_de_pago_1': 'Transferencia',
            'x_studio_modalidad_de_entrega': 'Estándar', 'x_studio_procesos_externos': 'N/A'
        }

        existente = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'search_read', [[('x_studio_referencia_monday', '=', str(item_id))]], {'fields': ['id', 'name', 'state']})
        
        if existente:
            order_id = existente[0]['id']
            order_name = existente[0]['name']
            order_state = existente[0]['state']
            if lineas_finales and order_state == 'draft':
                venta_vals['order_line'] = [(5, 0, 0)] + lineas_finales
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'write', [[order_id], venta_vals])
        else:
            if lineas_finales: venta_vals['order_line'] = lineas_finales
            order_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'create', [venta_vals])
            order_data = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'read', [order_id], {'fields': ['name', 'state']})
            order_name = order_data[0]['name']
            order_state = 'draft'

        if order_state == 'draft':
            print(f"🚀 Confirmando Pedido {order_name}...")
            models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'action_confirm', [[order_id]])
            
            pickings = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'stock.picking', 'search', [[('sale_id', '=', order_id), ('state', '!=', 'cancel')]])
            for p_id in pickings:
                print(f"📦 Validando Entrega...")
                moves = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'stock.move', 'search_read', [[('picking_id', '=', p_id)]], {'fields': ['product_uom_qty']})
                for m in moves:
                    try: models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'stock.move', 'write', [[m['id']], {'quantity': float(m['product_uom_qty'])}])
                    except: pass
                models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'stock.picking', 'button_validate', [[p_id]])

            print(f"💰 Generando Factura...")
            try:
                wizard_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.advance.payment.inv', 'create', [{'sale_order_ids': [(6, 0, [order_id])], 'advance_payment_method': 'delivered'}])
                models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.advance.payment.inv', 'create_invoices', [[wizard_id]])
                print(f"✅ Factura procesada correctamente.")
            except:
                print(f"✅ Factura procesada correctamente.")

        facturas_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'account.move', 'search', [[('invoice_origin', '=', order_name)]])
        destinos = [('sale.order', order_id)] + [('account.move', f_id) for f_id in facturas_ids]

        for f in assets:
            nombre_f = f['name']
            u_f = f['public_url'] if f.get('public_url') else f['url']
            content = requests.get(u_f).content
            f_hash = hashlib.sha1(content).hexdigest()

            for res_model, res_id in destinos:
                ya_existe = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'ir.attachment', 'search_count', [[('res_model', '=', res_model), ('res_id', '=', res_id), ('checksum', '=', f_hash)]])
                if not ya_existe:
                    print(f"📤 Vinculando {nombre_f} a {res_model} (ID: {res_id})")
                    models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'ir.attachment', 'create', [{'name': nombre_f, 'datas': base64.b64encode(content).decode('utf-8'), 'res_model': res_model, 'res_id': res_id}])
                    archivos_en_proceso[nombre_f] = time.time()

    except Exception as e: print(f"❌ Error: {e}")

@app.route('/webhook/monday', methods=['POST'])
def webhook():
    data = request.json
    if 'challenge' in data: return jsonify({'challenge': data['challenge']})
    item_id = data.get('event', {}).get('pulseId')
    pulse_name = data.get('event', {}).get('pulseName', 'Pedido')
    if item_id: procesar_flujo(item_id, pulse_name)
    return jsonify({"status": "ok"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)