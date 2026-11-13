AI VIDEO FACTORY RU — STAGE 5.2
Панель ручной оценки производственной пригодности тем
Исправление: множественный выбор через флажки (без Ctrl)
Версия: 1.0.1 / 03.11.2026

ЗАВИСИМОСТИ
- Основной проект video-farm (CLI, src-layout).
- Установленный Stage 4 (discover-editor).
- Установленный Stage 5.1 v1.0.1, включая config/production_profiles.json.
- Python .venv 3.12/3.13.

ПЕРЕД ОБНОВЛЕНИЕМ
В старом редакторе нажмите «Экспортировать отбор JSON» и сохраните editorial.json. Это нужно, чтобы перенести уже сделанные ручные отметки в пересозданный HTML.

УСТАНОВКА
1. Распакуйте ZIP в отдельную папку.
2. Откройте PowerShell в корне проекта video-farm.
3. Выполните:
   Set-ExecutionPolicy -Scope Process Bypass
   D:\stage5_2_package\install_stage5_2.ps1 -ProjectRoot "D:\my_projects\AI_Video_Factory_RU\video-farm"
   (Замените путь к папке пакета и корню проекта на свои.)

Установщик запускается через Python из .venv проекта. Он делает резервную копию трёх
изменяемых файлов, копирует обновлённый editor, проверяет production_profiles.json,
компилирует модуль, запускает регрессионные тесты Stage 3–5.2 и проверяет CLI.
При ошибке выполняет откат. .env, ключи API, исходные Discovery JSON, editorial.json
и пользовательские выходные данные не меняются. Вложенный config/production_profiles.json
служит тестовым справочником пакета; он не копируется поверх установленного Stage 5.1.

ЗАПУСК И ПЕРЕНОС ОТБОРА
1. Выполните: .\.venv\Scripts\python.exe -m kadr_farm.cli discover-editor
2. Откройте output\discovery\editor.html.
3. Нажмите «Импортировать отбор» и выберите ранее сохранённый editorial.json. Источник Discovery должен быть тем же самым.

МНОЖЕСТВЕННЫЙ ВЫБОР
В «Блокерах» и «Стратегии материалов» используются флажки. Выбирайте несколько пунктов обычными щелчками; Ctrl удерживать не нужно. Значения по-прежнему экспортируются как массивы в editorial.json (schema 1.1).

ЗАПУСК (повторная генерация)
.\.venv\Scripts\python.exe -m kadr_farm.cli discover-editor

ОТКРЫТЬ
Откройте output\discovery\editor.html в браузере. Страница не отправляет запросы
в Интернет. Сохраните редакторский отбор через кнопку экспорта; после обновления
Discovery импортируйте editorial.json только в панель для соответствующего исходного
отчёта и до его срока хранения.

ОБРАТНАЯ СОВМЕСТИМОСТЬ
Новый экспорт editorial.json имеет schema_version 1.1. Импорт поддерживает старую
Stage 4 schema_version 1.0, сохраняя decision, editor_note, language,
format_preference, reviewed и themes. Новые поля получают безопасные значения по умолчанию.

ОГРАНИЧЕНИЯ
- Все новые редакторские метки — ручные; классификатора по YouTube заголовкам нет.
- reference_format — формат найденного ролика; production_profile — формат нашего
  самостоятельного ролика по теме.
- Ни видеогенерация, ни Blender, ни screen recording, ни автопубликация не включены.
- Stage 5.2 не подтверждает Windows acceptance Stage 5.1 и полную готовность MVP.
