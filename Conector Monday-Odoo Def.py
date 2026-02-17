import xmlrpc.client
import requests
import json
from flask import Flask, request, jsonify
from datetime import datetime

app = Flask(__name__)

# ==========================================
#        CONFIGURACIÓN DE CREDENCIALES
# ==========================================
ODOO_URL = 'https://assemblik.odoo.com'
ODOO_DB = 'assemblik'
ODOO_USER = 'danbrito.mx@gmail.com'
ODOO_API_KEY = '99a8a2a919186d115acb4cbda5db5b9f6932ed18'

MONDAY_API_KEY = 'eyJhbGciOiJIUzI1NiJ9.eyJ0aWQiOjE3OTE5NTA3OCwiYWFpIjoxMSwidWlkIjoyNzY0MDgxOCwiaWFkIjoiMjAyMi0wOS0wNVQxNTo1MjoxOS4wMDBaIiwicGVyIjoibWU6d3JpdGUiLCJhY3RpZCI6MTEwODIyMzksInJnbiI6InVzZTEifQ.p2h7mcyZxo6SQWxGx_UUotKx7QvHClt2V1l7mwmfkwU' 
MONDAY_API_URL = "https://api.monday.com/v2"

# ==========================================
#       FUNCIONES DE COMUNICACIÓN
# ==========================================

def obtener_detalles_monday(item_id):
    headers = {
        "Authorization": MONDAY_API_KEY,
        "Content-Type": "application/json",
        "API-Version": "2023-10"
    }
    query = """
    query ($id: [ID!]!) {
      items (ids: $id) {
        name
        column_values {
          id
          text
        }
      }
    }
    """
    variables = {'id': [str(item_id)]}
    try:
        response = requests.post(MONDAY_API_URL, json={'query': query, 'variables': variables}, headers=headers)
        res_json = response.json()
        return res_json['data']['items'][0] if res_json.get('data') else None
    except Exception as e:
        print(f"❌ Error API Monday: {e}")
        return None

def crear_venta_en_odoo(detalles_monday, item_id):
    try:
        common = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/common')
        uid = common.authenticate(ODOO_DB, ODOO_USER, ODOO_API_KEY, {})
        models = xmlrpc.client.ServerProxy(f'{ODOO_URL}/xmlrpc/2/object')

        # Extraemos y limpiamos datos de Monday
        cols = {cv['id']: cv['text'] for cv in detalles_monday['column_values']}
        nombre_item_monday = detalles_monday['name'].strip() if detalles_monday['name'] else ""
        nombre_cliente     = cols.get('cliente', '').strip()
        num_cotizacion     = cols.get('texto5', '').strip()
        id_monday_col      = cols.get('id__de_elemento8', str(item_id)).strip()
        vendedor_final     = cols.get('personas', '').strip()

        # --- Lógica del Mes ---
        meses = {1: "Enero", 2: "Febrero", 3: "Marzo", 4: "Abril", 5: "Mayo", 6: "Junio", 
                 7: "Julio", 8: "Agosto", 9: "Septiembre", 10: "Octubre", 11: "Noviembre", 12: "Diciembre"}
        mes_actual = meses[datetime.now().month]

        # 1. Buscar Cliente (Partner)
        partner_id = 2 
        if nombre_cliente:
            p_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'res.partner', 'search', [[['name', '=', nombre_cliente]]])
            if p_ids: partner_id = p_ids[0]

        # 2. Buscar o Crear Proyecto
        proyecto_id = False
        if nombre_item_monday:
            proj_ids = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'search', [[['name', '=', nombre_item_monday]]])
            if proj_ids:
                proyecto_id = proj_ids[0]
            else:
                proyecto_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'project.project', 'create', [{'name': nombre_item_monday}])

        # 3. Preparar valores para Odoo
        venta_vals = {
            'partner_id': partner_id,
            'x_studio_vendedor': vendedor_final,
            'x_studio_referencia_monday': id_monday_col,
            'x_studio_many2one_field_dovxQ': proyecto_id,
            'x_studio_cotizacin': num_cotizacion,
            'x_studio_material_1': 'Incluido',
            'origin': f'Monday ID: {item_id}',
            'x_studio_mes_de_venta': mes_actual,
            'x_studio_modalidad_de_entrega': 'Estándar', 
            'x_studio_procesos_externos': 'N/A',
            'x_studio_tipo_1': 'Maquila',
            'x_studio_ubicacin': 'Querétaro', 
            'x_studio_metodo_de_pago_1': 'Transferencia',
            'x_studio_facturacin': 'Factura'
        }

        print(f"DEBUG: Intentando crear para {vendedor_final}...")
        
        try:
            # Intento de creación principal
            venta_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'create', [venta_vals])
            print(f"✅ ÉXITO TOTAL: Presupuesto {venta_id} creado con vendedor.")
            return venta_id
        except Exception as e_inner:
            # Respaldo: Si el nombre del vendedor falla, crea la venta sin él para no bloquear el proceso
            print(f"⚠️ Error de validación en vendedor '{vendedor_final}': {e_inner}")
            if 'x_studio_vendedor' in venta_vals:
                del venta_vals['x_studio_vendedor']
                venta_id = models.execute_kw(ODOO_DB, uid, ODOO_API_KEY, 'sale.order', 'create', [venta_vals])
                print(f"✅ Venta creada SIN vendedor (ID: {venta_id}). Revisa el valor técnico en Odoo Studio.")
                return venta_id
            raise e_inner

    except Exception as e:
        print(f"❌ Error crítico en Odoo: {e}")
        return None

# ==========================================
#           RUTA DEL WEBHOOK (FLASK)
# ==========================================

@app.route('/webhook/monday', methods=['POST'])
def monday_webhook():
    data = request.json
    if not data: return jsonify({"status": "error"}), 400
    if 'challenge' in data: return jsonify({'challenge': data['challenge']})

    event = data.get('event', {})
    item_id = event.get('pulseId')

    if item_id:
        print(f"🔔 Procesando ID Monday: {item_id}")
        detalles = obtener_detalles_monday(item_id)
        if detalles:
            odoo_id = crear_venta_en_odoo(detalles, item_id)
            if odoo_id:
                return jsonify({"status": "success", "odoo_id": odoo_id}), 200
    
    return jsonify({"status": "processed"}), 200

if __name__ == '__main__':
    app.run(host='0.0.0.0', port=5000)