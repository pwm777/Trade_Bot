"""
Timestamp Diagnostic Tool
Диагностика и исправление проблем с timestamp в базе данных
"""

import sqlite3
import pandas as pd
import logging
from pathlib import Path
from datetime import datetime
import struct

# Настройка логирования
logging.basicConfig(level=logging.INFO, format='%(asctime)s - %(levelname)s - %(message)s')
logger = logging.getLogger(__name__)


class TimestampDiagnostic:
    def __init__(self, db_path: str = "data/market_data.sqlite"):
        self.db_path = Path(db_path)
        self.conn = None

    def connect(self):
        """Подключение к базе данных"""
        if not self.db_path.exists():
            raise FileNotFoundError(f"База данных не найдена: {self.db_path}")

        self.conn = sqlite3.connect(self.db_path)
        logger.info(f"✅ Подключено к базе: {self.db_path}")

    def disconnect(self):
        """Закрытие соединения"""
        if self.conn:
            self.conn.close()
            logger.info("✅ Соединение с базой закрыто")

    def run_complete_diagnosis(self):
        """Полная диагностика проблем с timestamp"""
        print("\n" + "=" * 60)
        print("🔍 ПОЛНАЯ ДИАГНОСТИКА TIMESTAMP")
        print("=" * 60)

        self.connect()

        try:
            # 1. Базовая статистика
            self._basic_stats()

            # 2. Анализ типов данных
            self._type_analysis()

            # 3. Детальный анализ значений
            self._detailed_value_analysis()

            # 4. Анализ по методам разметки
            self._method_analysis()

            # 5. Предложения по исправлению
            self._suggest_fixes()

        finally:
            self.disconnect()

    def _basic_stats(self):
        """Базовая статистика таблицы"""
        print("\n📊 БАЗОВАЯ СТАТИСТИКА:")

        query = """
        SELECT 
            COUNT(*) as total_records,
            COUNT(DISTINCT symbol) as unique_symbols,
            MIN(timestamp) as min_timestamp,
            MAX(timestamp) as max_timestamp,
            MIN(extreme_timestamp) as min_extreme_ts,
            MAX(extreme_timestamp) as max_extreme_ts
        FROM labeling_results
        """

        df = pd.read_sql_query(query, self.conn)
        total = df.iloc[0]['total_records']

        print(f"   • Всего записей: {total}")
        print(f"   • Уникальных символов: {df.iloc[0]['unique_symbols']}")
        print(f"   • Timestamp диапазон: {df.iloc[0]['min_timestamp']} - {df.iloc[0]['max_timestamp']}")
        print(f"   • Extreme timestamp диапазон: {df.iloc[0]['min_extreme_ts']} - {df.iloc[0]['max_extreme_ts']}")

    def _type_analysis(self):
        """Анализ типов данных timestamp"""
        print("\n🔧 АНАЛИЗ ТИПОВ ДАННЫХ:")

        query = """
        SELECT 
            typeof(timestamp) as timestamp_type,
            typeof(extreme_timestamp) as extreme_timestamp_type,
            COUNT(*) as count
        FROM labeling_results 
        GROUP BY typeof(timestamp), typeof(extreme_timestamp)
        """

        df = pd.read_sql_query(query, self.conn)

        if df.empty:
            print("   ❌ Нет данных в таблице")
            return

        for _, row in df.iterrows():
            ts_type = row['timestamp_type']
            extreme_type = row['extreme_timestamp_type']
            count = row['count']

            status = "✅" if ts_type == 'integer' and extreme_type == 'integer' else "❌"
            print(f"   {status} {ts_type}/{extreme_type}: {count} записей")

    def _detailed_value_analysis(self):
        """Детальный анализ значений"""
        print("\n📋 ДЕТАЛЬНЫЙ АНАЛИЗ ЗНАЧЕНИЙ:")

        # Смотрим первые 10 записей
        query = """
        SELECT 
            rowid,
            symbol,
            timestamp,
            typeof(timestamp) as ts_type,
            hex(timestamp) as ts_hex,
            extreme_timestamp, 
            typeof(extreme_timestamp) as extreme_ts_type,
            hex(extreme_timestamp) as extreme_ts_hex,
            reversal_label,
            labeling_method
        FROM labeling_results 
        LIMIT 10
        """

        df = pd.read_sql_query(query, self.conn)

        if df.empty:
            print("   ❌ Нет данных для анализа")
            return

        print("   Первые 10 записей:")
        for _, row in df.iterrows():
            print(f"   --- RowID: {row['rowid']} ---")
            print(f"      Symbol: {row['symbol']}")
            print(f"      Timestamp: {row['timestamp']} (type: {row['ts_type']})")
            print(f"      Timestamp HEX: {row['ts_hex']}")
            print(f"      Extreme TS: {row['extreme_timestamp']} (type: {row['extreme_ts_type']})")
            print(f"      Extreme TS HEX: {row['extreme_ts_hex']}")
            print(f"      Method: {row['labeling_method']}")

    def _method_analysis(self):
        """Анализ по методам разметки"""
        print("\n🎯 АНАЛИЗ ПО МЕТОДАМ РАЗМЕТКИ:")

        query = """
        SELECT 
            labeling_method,
            COUNT(*) as count,
            typeof(timestamp) as ts_type,
            typeof(extreme_timestamp) as extreme_ts_type
        FROM labeling_results 
        GROUP BY labeling_method, typeof(timestamp), typeof(extreme_timestamp)
        ORDER BY labeling_method, count DESC
        """

        df = pd.read_sql_query(query, self.conn)

        if df.empty:
            print("   ❌ Нет данных по методам")
            return

        current_method = None
        for _, row in df.iterrows():
            method = row['labeling_method']
            if method != current_method:
                print(f"   📁 {method}:")
                current_method = method

            status = "✅" if row['ts_type'] == 'integer' and row['extreme_ts_type'] == 'integer' else "❌"
            print(f"      {status} {row['ts_type']}/{row['extreme_ts_type']}: {row['count']} записей")

    def _suggest_fixes(self):
        """Предложения по исправлению"""
        print("\n🔧 ПРЕДЛОЖЕНИЯ ПО ИСПРАВЛЕНИЮ:")

        # Проверяем сколько записей с проблемами
        query = """
        SELECT COUNT(*) as problematic_count 
        FROM labeling_results 
        WHERE typeof(timestamp) != 'integer' 
           OR typeof(extreme_timestamp) != 'integer'
        """

        df = pd.read_sql_query(query, self.conn)
        problematic_count = df.iloc[0]['problematic_count']

        if problematic_count == 0:
            print("   ✅ Проблемных записей не обнаружено")
            return

        print(f"   ❌ Обнаружено {problematic_count} проблемных записей")
        print("\n   💡 РЕКОМЕНДАЦИИ:")

        # Анализируем типы проблем
        type_query = """
        SELECT 
            typeof(timestamp) as ts_type,
            typeof(extreme_timestamp) as extreme_ts_type,
            COUNT(*) as count
        FROM labeling_results 
        WHERE typeof(timestamp) != 'integer' OR typeof(extreme_timestamp) != 'integer'
        GROUP BY ts_type, extreme_ts_type
        """

        type_df = pd.read_sql_query(type_query, self.conn)

        for _, row in type_df.iterrows():
            ts_type = row['ts_type']
            extreme_type = row['extreme_ts_type']
            count = row['count']

            if ts_type == 'blob' or extreme_type == 'blob':
                print(f"   • {count} записей с BLOB данными - нужно конвертировать в INTEGER")
            elif ts_type == 'text' or extreme_type == 'text':
                print(f"   • {count} записей с TEXT данными - нужно преобразовать в INTEGER")
            else:
                print(f"   • {count} записей с типом {ts_type}/{extreme_type} - требуется ручное исправление")

    def quick_fix_blob_timestamps(self):
        """Быстрое исправление BLOB timestamp"""
        print("\n⚡ БЫСТРОЕ ИСПРАВЛЕНИЕ BLOB TIMESTAMP...")

        self.connect()

        try:
            # Создаем бэкап
            self.conn.execute("CREATE TABLE IF NOT EXISTS labeling_results_backup AS SELECT * FROM labeling_results")

            # Исправляем BLOB в INTEGER
            fix_query = """
            UPDATE labeling_results 
            SET timestamp = CAST(timestamp AS INTEGER),
                extreme_timestamp = CAST(extreme_timestamp AS INTEGER)
            WHERE typeof(timestamp) = 'blob' OR typeof(extreme_timestamp) = 'blob'
            """

            cursor = self.conn.execute(fix_query)
            fixed_count = cursor.rowcount
            self.conn.commit()

            print(f"   ✅ Исправлено {fixed_count} записей")

            # Проверяем результат
            check_query = """
            SELECT COUNT(*) as remaining_problems
            FROM labeling_results 
            WHERE typeof(timestamp) != 'integer' OR typeof(extreme_timestamp) != 'integer'
            """

            result = self.conn.execute(check_query).fetchone()
            remaining = result[0] if result else 0

            if remaining == 0:
                print("   🎉 Все проблемы исправлены!")
            else:
                print(f"   ⚠️ Осталось {remaining} проблемных записей")

        except Exception as e:
            self.conn.rollback()
            print(f"   ❌ Ошибка при исправлении: {e}")
        finally:
            self.disconnect()

    def emergency_fix_all_timestamps(self):
        """Экстренное исправление всех timestamp (использует rowid)"""
        print("\n🚨 ЭКСТРЕННОЕ ИСПРАВЛЕНИЕ ВСЕХ TIMESTAMP...")

        confirm = input("   ⚠️  Это перезапишет ВСЕ timestamp! Продолжить? (y/N): ")
        if confirm.lower() != 'y':
            print("   ❌ Отменено пользователем")
            return

        self.connect()

        try:
            # Создаем бэкап
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS labeling_results_backup_emergency AS SELECT * FROM labeling_results")

            # Исправляем ВСЕ timestamp используя rowid
            base_timestamp = 1609459200000  # 2021-01-01 в milliseconds

            fix_query = """
            UPDATE labeling_results 
            SET timestamp = (rowid * 60000) + ?,
                extreme_timestamp = (rowid * 60000) + ?
            """

            cursor = self.conn.execute(fix_query, (base_timestamp, base_timestamp))
            fixed_count = cursor.rowcount
            self.conn.commit()

            print(f"   ✅ Исправлено {fixed_count} записей")
            print("   📅 Новые timestamp начинаются с 2021-01-01")

        except Exception as e:
            self.conn.rollback()
            print(f"   ❌ Ошибка при исправлении: {e}")
        finally:
            self.disconnect()


def main():
    """Главное меню диагностики"""
    diagnostic = TimestampDiagnostic()

    while True:
        print("\n" + "=" * 50)
        print("           TIMESTAMP DIAGNOSTIC TOOL")
        print("=" * 50)
        print("[1] Полная диагностика")
        print("[2] Быстрое исправление BLOB timestamp")
        print("[3] ЭКСТРЕННОЕ исправление всех timestamp")
        print("[4] Проверить исправления")
        print("[0] Выход")

        choice = input("\nВаш выбор: ").strip()

        if choice == '1':
            diagnostic.run_complete_diagnosis()
        elif choice == '2':
            diagnostic.quick_fix_blob_timestamps()
        elif choice == '3':
            diagnostic.emergency_fix_all_timestamps()
        elif choice == '4':
            diagnostic.run_complete_diagnosis()
        elif choice == '0':
            print("👋 До свидания!")
            break
        else:
            print("❌ Неверный выбор")


if __name__ == "__main__":
    main()