# 🔴 CORRECCIÓN CRÍTICA: db.py Header Storage Strategy

## ❌ Problema Detectado por el Usuario

Después de AuxPOW activation (bloque 1614560), habrá **DOS tipos de bloques**:

1. **Bloques AuxPOW** (merge-mined con Litecoin/Dogecoin):
   - Version bit `0x100` SET
   - Header: 80 bytes
   - Algoritmo: Scrypt

2. **Bloques MeowPow** (minados directamente):
   - Version bit `0x100` CLEAR  
   - Header: 120 bytes
   - Algoritmo: MeowPow/KAWPOW

### El Problema:

```python
# electrumx/server/db.py - flush_fs()
offset = self.header_offset(start_height)  # Calcula offset "estático"
self.headers_file.write(offset, b''.join(flush_data.headers))
```

Si almacenamos headers de **tamaños variables** (80 y 120 bytes mezclados), el método `static_header_offset()` **NO PUEDE** calcular offsets correctamente porque no sabe qué bloques son AuxPOW.

---

## ✅ Solución Implementada: PADDING

**IGUAL que hace Electrum Wallet** (blockchain.py línea 543):

```python
# Almacenar TODOS los headers como 120 bytes
if hdr_len == LEGACY_HEADER_SIZE:
    r += chunk[p:p + hdr_len] + bytes(40)  # pad to 120 for storage
```

### En ElectrumX:

#### 1. **ESCRITURA (coins.py - block_header())**
```python
if is_auxpow:
    basic_header = block[:80]  # 80 bytes
    padding = bytes(40)        # 40 bytes de padding
    return basic_header + padding  # 120 bytes → AL ARCHIVO
```

#### 2. **LECTURA (db.py - read_headers())**
```python
headers_from_disk = self.headers_file.read(offset, size)  # 120 bytes cada uno
headers_unpadded = self._unpad_auxpow_headers(headers_from_disk, start_height)
# AuxPOW: 80 bytes → AL CLIENTE
# MeowPow: 120 bytes → AL CLIENTE
```

---

## 📊 Flujo de Datos Actualizado

### Bloque AuxPOW (ej: altura 1615000):

```
[Daemon]
├─ Envía: 80 bytes + AuxPOW data
└─ RPC →

[ElectrumX - Recepción]
├─ coin.block() parsea: 80 bytes (trunca AuxPOW data)
└─ block_processor.py →

[ElectrumX - Almacenamiento]
├─ coin.block_header() padea: 80 → 120 bytes
├─ db.flush_fs() escribe: 120 bytes AL DISCO
└─ headers_file contiene: 120 bytes

[ElectrumX - Envío]
├─ db.read_headers() lee: 120 bytes DEL DISCO
├─ _unpad_auxpow_headers() despadea: 120 → 80 bytes
├─ session.py envía: 80 bytes AL CLIENTE
└─ Protocol →

[Electrum Wallet]
└─ Recibe y verifica: 80 bytes ✅
```

### Bloque MeowPow (ej: altura 1615001):

```
[Daemon]
├─ Envía: 120 bytes (KAWPOW/MEOWPOW)
└─ RPC →

[ElectrumX - Todo el flujo]
├─ Parsea: 120 bytes
├─ Almacena: 120 bytes
├─ Lee: 120 bytes  
├─ NO despadea (ya es 120)
├─ Envía: 120 bytes
└─ Protocol →

[Electrum Wallet]
└─ Recibe y verifica: 120 bytes ✅
```

---

## 🎯 Por Qué Funciona:

### Offsets en Archivo headers_file:

```
Altura    | Tipo      | En Disco | En Memoria | Offset Cálculo
----------|-----------|----------|------------|----------------
0-372     | X16R      | 80 bytes | 80 bytes   | altura * 80
373-1614559 | KAWPOW  | 120 bytes| 120 bytes  | 373*80 + (h-373)*120
1614560   | AuxPOW    | 120 (pad)| 80 (unpad) | 373*80 + (h-373)*120 ✅
1614561   | MeowPow   | 120 bytes| 120 bytes  | 373*80 + (h-373)*120 ✅
1614562   | AuxPOW    | 120 (pad)| 80 (unpad) | 373*80 + (h-373)*120 ✅
1614563   | MeowPow   | 120 bytes| 120 bytes  | 373*80 + (h-373)*120 ✅
```

✅ **Offsets son ESTÁTICOS** porque todos los bloques >= 373 ocupan 120 bytes en disco
✅ **Clientes reciben tamaño correcto** porque se despadea al leer
✅ **hash_header() funciona** porque usa solo primeros 80 bytes de AuxPOW

---

## 📝 Métodos Agregados a db.py:

### 1. `_unpad_auxpow_header(header, height)`
```python
def _unpad_auxpow_header(self, header, height):
    '''Remove padding from single AuxPOW header'''
    if self.coin.is_auxpow_active(height):
        version_int = int.from_bytes(header[:4], byteorder='little')
        if version_int & (1 << 8):  # AuxPOW bit
            return header[:80]  # Remove 40 bytes padding
    return header
```

### 2. `_unpad_auxpow_headers(headers, start_height)`
```python
def _unpad_auxpow_headers(self, headers, start_height):
    '''Remove padding from multiple concatenated headers'''
    result = b''
    for each header in headers:
        read 120 bytes from file
        unpad if AuxPOW
        add to result
    return result
```

### 3. Modificado `fs_block_hashes()`
```python
# Ahora calcula hlen basándose en headers despadeados, no en disco
if is_auxpow_block:
    hlen = 80
else:
    hlen = header_len(h)  # 120
```

---

## ✅ VENTAJAS de esta solución:

1. ✅ **Offsets estáticos** - `static_header_offset()` funciona correctamente
2. ✅ **Compatible con Electrum** - wallet hace lo mismo (padding en storage)
3. ✅ **Merkle cache correcto** - hash basado en header sin padding
4. ✅ **Clientes reciben formato correcto** - 80 para AuxPOW, 120 para MeowPow
5. ✅ **Sin metadata adicional** - version bit es suficiente para detectar

---

## 📊 Comparación con Electrum Wallet:

| Aspecto | Electrum Wallet | ElectrumX Server | Match? |
|---------|----------------|------------------|--------|
| **Storage** | Pad AuxPOW 80→120 | Pad AuxPOW 80→120 | ✅ SÍ |
| **Envío** | Unpad 120→80 | Unpad 120→80 | ✅ SÍ |
| **Detección** | version bit + altura | version bit + altura | ✅ SÍ |
| **Offsets** | Estáticos (120 siempre >= KAWPOW) | Estáticos (120 siempre >= KAWPOW) | ✅ SÍ |

---

## 🔧 Archivos Modificados (ACTUALIZADO):

### electrumx-meowcoin/
1. ✅ `electrumx/lib/coins.py` - Padding en block_header()
2. ✅ `electrumx/lib/tx.py` - Parámetro height
3. ✅ `electrumx/server/block_processor.py` - Actualizar block.header
4. ✅ **`electrumx/server/db.py`** - ← **NUEVO: Unpadding al leer**

### electrum-meowcoin/
5. ✅ `electrum/blockchain.py` - Detección correcta de AuxPOW

---

**ESTADO**: ✅ **SINCRONIZACIÓN COMPLETA VERIFICADA CON db.py**

