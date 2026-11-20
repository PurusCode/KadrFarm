# KadrFarm — Visual Story V2 Implementation

## Назначение

Это добавочный исполнительный конвейер визуальной режиссуры.

Он не заменяет:

- `visual_story.py`;
- `visual_story_prompts.py`;
- `production_director.py`;
- `director_assembler.py`;
- `media_producer.py`;
- старый semantic pipeline.

## Модули

| Файл | Ответственность |
|---|---|
| visual_story_v2.py | Типизированные модели плана |
| visual_scene_compiler_v2.py | Расписание сцен и речевых событий |
| visual_asset_resolver_v2.py | Локальные материалы, URL и Pixabay |
| visual_renderer_v2.py | Графические композиции |
| visual_animation_v2.py | Анимация и генерация кадров |
| visual_execution_v2.py | WAV, FFmpeg, MP4 |
| visual_audit_v2.py | Контактный лист и аудит |
| visual_v2_cli.py | Запуск нового конвейера |

## Установка

Распаковать файлы в корень KadrFarm.

Убедиться, что установлены зависимости проекта:

- Pydantic 2;
- Pillow;
- Kokoro production dependencies;
- FFmpeg;
- FFprobe.

Из корня проекта:

```powershell
python -m pip install -e .