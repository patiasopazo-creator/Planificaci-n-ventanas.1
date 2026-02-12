#!/usr/bin/env python3
"""
Script de verificación pre-lanzamiento
Valida que todo esté configurado correctamente antes de ejecutar la app
"""

import os
import sys
from pathlib import Path

def check_files():
    """Verifica que todos los archivos necesarios existan."""
    print("=" * 60)
    print("📋 VERIFICACIÓN DE ARCHIVOS")
    print("=" * 60)
    
    required_files = {
        "main.py": "Aplicación principal",
        ".streamlit/planificacion_data.db": "Base de datos SQLite",
    }
    
    all_ok = True
    for file_path, description in required_files.items():
        exists = Path(file_path).exists()
        status = "✅" if exists else "❌"
        print(f"{status} {file_path:40} {description}")
        if not exists and ".db" not in file_path:
            all_ok = False
    
    return all_ok

def check_secrets():
    """Verifica que secrets.toml tenga las variables necesarias."""
    print("\n" + "=" * 60)
    print("🔐 VERIFICACIÓN DE CONFIGURACIÓN")
    print("=" * 60)
    
    secrets_path = Path(".streamlit/secrets.toml")
    
    if not secrets_path.exists():
        print("❌ No se encontró .streamlit/secrets.toml")
        return False
    
    try:
        with open(secrets_path, 'r', encoding='utf-8') as f:
            content = f.read()
    except Exception as e:
        print(f"❌ Error al leer secrets.toml: {e}")
        return False
    
    required_keys = ["SIMPLE_LOGIN_USER", "SIMPLE_LOGIN_PASSWORD"]
    all_ok = True
    
    for key in required_keys:
        if key in content and f"{key} =" in content:
            print(f"✅ {key:30} configurado")
        else:
            print(f"❌ {key:30} FALTA O VACÍO")
            all_ok = False
    
    return all_ok

def check_dependencies():
    """Verifica que las dependencias estén instaladas."""
    print("\n" + "=" * 60)
    print("📦 VERIFICACIÓN DE DEPENDENCIAS")
    print("=" * 60)
    
    required_packages = {
        "streamlit": "Framework web",
        "pandas": "Análisis de datos",
        "plotly": "Gráficos",
        "sqlite3": "Base de datos",
    }
    
    all_ok = True
    for package, description in required_packages.items():
        try:
            __import__(package)
            print(f"✅ {package:20} {description}")
        except ImportError:
            print(f"❌ {package:20} FALTA INSTALAR")
            all_ok = False
    
    return all_ok

def main():
    """Ejecuta todas las verificaciones."""
    print("\n")
    print("🔍 VERIFICACIÓN PRE-LANZAMIENTO")
    print("Planificación Ventanas - Login Simple")
    print()
    
    files_ok = check_files()
    deps_ok = check_dependencies()
    secrets_ok = check_secrets()
    
    print("\n" + "=" * 60)
    print("📊 RESUMEN")
    print("=" * 60)
    
    results = [
        ("Archivos", files_ok),
        ("Dependencias", deps_ok),
        ("Configuración", secrets_ok),
    ]
    
    for name, status in results:
        icon = "✅" if status else "❌"
        print(f"{icon} {name}")
    
    if all(status for _, status in results):
        print("\n" + "=" * 60)
        print("✅ ¡TODO LISTO!")
        print("=" * 60)
        print("\nPuedes ejecutar:")
        print("  streamlit run main.py")
        print()
        return 0
    else:
        print("\n" + "=" * 60)
        print("❌ FALTAN CONFIGURACIONES")
        print("=" * 60)
        print("\nAsegúrate de que .streamlit/secrets.toml tenga:")
        print("  SIMPLE_LOGIN_USER = (valor configurado)")
        print("  SIMPLE_LOGIN_PASSWORD = (valor configurado)")
        print()
        return 1

if __name__ == "__main__":
    sys.exit(main())
