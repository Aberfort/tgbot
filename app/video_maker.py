"""Модуль автоматичного монтажу вертикальних TikTok/Reels/Shorts відео (9:16) за допомогою Pillow та FFmpeg."""

from __future__ import annotations

import asyncio
import logging
import re
import textwrap
from pathlib import Path
from typing import Sequence

from PIL import Image, ImageDraw, ImageFilter, ImageFont

logger = logging.getLogger(__name__)

# Оптимальна HD-роздільність 9:16 для TikTok/Reels (швидкий рендер і мале споживання RAM у контейнері)
VIDEO_W = 720
VIDEO_H = 1280

FONT_BOLD_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans-Bold.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Bold.ttf",
    "/System/Library/Fonts/Supplemental/Arial Bold.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)

FONT_REGULAR_CANDIDATES = (
    "/usr/share/fonts/truetype/dejavu/DejaVuSans.ttf",
    "/usr/share/fonts/truetype/liberation/LiberationSans-Regular.ttf",
    "/System/Library/Fonts/Supplemental/Arial.ttf",
    "/System/Library/Fonts/Helvetica.ttc",
)


def _load_font(candidates: tuple[str, ...], size: int) -> ImageFont.FreeTypeFont | ImageFont.ImageFont:
    """Завантажує перший доступний кириличний TTF-шрифт із системи."""
    for path_str in candidates:
        p = Path(path_str)
        if p.exists():
            try:
                return ImageFont.truetype(str(p), size=size)
            except Exception:
                continue
    return ImageFont.load_default()


def _strip_unsupported_chars(text: str) -> str:
    """Видаляє емодзі та спецсимволи, які стандартні Linux TTF-шрифти відображають як квадратики."""
    # Залишаємо кирилицю, латиницю, цифри, базову пунктуацію та пробіли
    cleaned = re.sub(r"[^\w\s\.,!?\-:;\"'«»()@#%&/+\-–—№$]", "", text, flags=re.UNICODE)
    cleaned = re.sub(r"\s+", " ", cleaned).strip()
    return cleaned


def build_overlay_png(
    output_path: Path,
    hook_title: str,
    sub_cta: str = "Подробности и HD-фото уже в нашем ТГ",
    channel_handle: str = "@btsdailyhub",
) -> Path:
    """Створює прозорий RGBA-оверлей 720x1280 із заголовком-хуком зверху та CTA-плашкою каналу знизу."""
    img = Image.new("RGBA", (VIDEO_W, VIDEO_H), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)

    # 1. Верхній і нижній м'які темні градієнти для ідеальної читабельності тексту на будь-якому фоні
    top_grad_h = 370
    for y in range(top_grad_h):
        alpha = int(205 * (1 - y / top_grad_h) ** 1.35)
        draw.line([(0, y), (VIDEO_W, y)], fill=(10, 6, 20, alpha))

    bot_grad_h = 360
    bot_start = VIDEO_H - bot_grad_h
    for i in range(bot_grad_h):
        y = bot_start + i
        alpha = int(215 * (i / bot_grad_h) ** 1.25)
        draw.line([(0, y), (VIDEO_W, y)], fill=(10, 6, 20, alpha))

    font_tag = _load_font(FONT_BOLD_CANDIDATES, 22)
    font_hook = _load_font(FONT_BOLD_CANDIDATES, 38)
    font_sub = _load_font(FONT_REGULAR_CANDIDATES, 24)
    font_cta = _load_font(FONT_BOLD_CANDIDATES, 28)

    # 2. Верхня фіолетова плашка-бейдж (у безпечній зоні TikTok, нижче системного статус-бару)
    tag_text = "BTS DAILY HUB  •  NEWS"
    tag_bbox = draw.textbbox((0, 0), tag_text, font=font_tag)
    tag_w = tag_bbox[2] - tag_bbox[0]
    tag_h = tag_bbox[3] - tag_bbox[1]
    pad_x, pad_y = 22, 10
    tag_x1 = (VIDEO_W - tag_w) // 2 - pad_x
    tag_y1 = 105
    tag_x2 = (VIDEO_W + tag_w) // 2 + pad_x
    tag_y2 = tag_y1 + tag_h + pad_y * 2
    draw.rounded_rectangle(
        [tag_x1, tag_y1, tag_x2, tag_y2],
        radius=20,
        fill=(139, 92, 246, 235),  # Фірмовий фіолетовий ARMY
    )
    draw.text(
        ((VIDEO_W - tag_w) // 2, tag_y1 + pad_y - 2),
        tag_text,
        font=font_tag,
        fill=(255, 255, 255, 255),
    )

    # 3. Заголовок-хук (2-3 рядки по центру з м'якою тінню)
    clean_hook = _strip_unsupported_chars(hook_title) or "Срочные новости BTS!"
    wrapped_lines = textwrap.wrap(clean_hook, width=26)[:3]
    current_y = tag_y2 + 24
    for line in wrapped_lines:
        bbox = draw.textbbox((0, 0), line, font=font_hook)
        line_w = bbox[2] - bbox[0]
        line_h = bbox[3] - bbox[1]
        x = (VIDEO_W - line_w) // 2
        # Контур/тінь для контрастності
        for dx, dy in ((-2, 2), (2, 2), (0, 3), (-1, -1), (1, -1)):
            draw.text((x + dx, current_y + dy), line, font=font_hook, fill=(0, 0, 0, 190))
        draw.text((x, current_y), line, font=font_hook, fill=(255, 255, 255, 255))
        current_y += line_h + 14

    # 4. Нижній блок заклику перейти в Telegram (вище зони опису інтерфейсу TikTok)
    clean_sub = _strip_unsupported_chars(sub_cta) or "Подробности и HD-фото в нашем ТГ"
    sub_lines = textwrap.wrap(clean_sub, width=38)[:2]
    sub_y = 955
    for line in sub_lines:
        bbox = draw.textbbox((0, 0), line, font=font_sub)
        lw = bbox[2] - bbox[0]
        lh = bbox[3] - bbox[1]
        x = (VIDEO_W - lw) // 2
        draw.text((x, sub_y + 2), line, font=font_sub, fill=(0, 0, 0, 190))
        draw.text((x, sub_y), line, font=font_sub, fill=(238, 232, 255, 255))
        sub_y += lh + 8

    # 5. Фірмова CTA-кнопка з назвою ТГ-каналу
    cta_text = f"ТГ-канал: {channel_handle}"
    cta_bbox = draw.textbbox((0, 0), cta_text, font=font_cta)
    cta_w = cta_bbox[2] - cta_bbox[0]
    cta_h = cta_bbox[3] - cta_bbox[1]
    c_pad_x, c_pad_y = 30, 14
    cx1 = (VIDEO_W - cta_w) // 2 - c_pad_x
    cy1 = max(sub_y + 14, 1015)
    cx2 = (VIDEO_W + cta_w) // 2 + c_pad_x
    cy2 = cy1 + cta_h + c_pad_y * 2

    # Легка тінь під кнопкою
    draw.rounded_rectangle(
        [cx1 + 2, cy1 + 4, cx2 + 2, cy2 + 4],
        radius=26,
        fill=(0, 0, 0, 130),
    )
    draw.rounded_rectangle(
        [cx1, cy1, cx2, cy2],
        radius=26,
        fill=(124, 58, 237, 245),
        outline=(221, 214, 254, 220),
        width=2,
    )
    draw.text(
        ((VIDEO_W - cta_w) // 2, cy1 + c_pad_y - 2),
        cta_text,
        font=font_cta,
        fill=(255, 255, 255, 255),
    )

    output_path.parent.mkdir(parents=True, exist_ok=True)
    img.save(output_path, format="PNG")
    return output_path


def _prepare_photo_slide(photo_path: Path, out_path: Path) -> Path:
    """Компонує одне фото у вертикальний 720x1280 кадр (розмитий фон + чітке фото по центру)."""
    with Image.open(photo_path) as src:
        rgb = src.convert("RGB")
        sw, sh = rgb.size

        # 1. Розмитий фон на весь екран 9:16
        bg_scale = max(VIDEO_W / sw, VIDEO_H / sh)
        bg_w, bg_h = int(sw * bg_scale), int(sh * bg_scale)
        bg = rgb.resize((bg_w, bg_h), Image.Resampling.LANCZOS)
        left = (bg_w - VIDEO_W) // 2
        top = (bg_h - VIDEO_H) // 2
        bg = bg.crop((left, top, left + VIDEO_W, top + VIDEO_H))
        bg = bg.filter(ImageFilter.GaussianBlur(radius=22))

        # Легке затемнення фону
        dark_layer = Image.new("RGB", (VIDEO_W, VIDEO_H), (12, 8, 22))
        bg = Image.blend(bg, dark_layer, alpha=0.25)

        # 2. Чітке переднє зображення з пропорційним масштабуванням
        fg_scale = min(VIDEO_W / sw, (VIDEO_H - 120) / sh)
        fg_w, fg_h = int(sw * fg_scale), int(sh * fg_scale)
        fg = rgb.resize((fg_w, fg_h), Image.Resampling.LANCZOS)

        paste_x = (VIDEO_W - fg_w) // 2
        paste_y = (VIDEO_H - fg_h) // 2
        bg.paste(fg, (paste_x, paste_y))
        bg.save(out_path, format="JPEG", quality=92)
    return out_path


async def _run_ffmpeg(args: list[str], timeout_sec: int = 90) -> None:
    """Асинхронно запускає FFmpeg із контролем таймауту та логуванням помилок."""
    proc = await asyncio.create_subprocess_exec(
        "ffmpeg",
        "-y",
        "-hide_banner",
        "-loglevel",
        "error",
        *args,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=timeout_sec)
    except asyncio.TimeoutError:
        proc.kill()
        await proc.wait()
        raise RuntimeError(f"FFmpeg перевищив таймаут ({timeout_sec} сек)")

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="ignore").strip()
        raise RuntimeError(f"FFmpeg завершився з кодом {proc.returncode}: {err_msg}")


async def render_vertical_from_video(
    video_path: Path,
    overlay_path: Path,
    output_path: Path,
    max_duration: int = 15,
) -> Path:
    """Рендерить вертикальне 9:16 відео з вихідного відеофайлу (розмитий фон + оверлей + обрізка до 15с)."""
    filter_complex = (
        f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=increase,"
        f"crop={VIDEO_W}:{VIDEO_H},boxblur=18:5[bg];"
        f"[0:v]scale={VIDEO_W}:{VIDEO_H}:force_original_aspect_ratio=decrease[fg];"
        f"[bg][fg]overlay=(W-w)/2:(H-h)/2[base];"
        f"[base][1:v]overlay=0:0[out]"
    )
    args = [
        "-i", str(video_path),
        "-i", str(overlay_path),
        "-t", str(max_duration),
        "-filter_complex", filter_complex,
        "-map", "[out]",
        "-map", "0:a?",
        "-c:v", "libx264",
        "-preset", "veryfast",
        "-crf", "23",
        "-pix_fmt", "yuv420p",
        "-r", "30",
        "-c:a", "aac",
        "-b:a", "128k",
        "-movflags", "+faststart",
        str(output_path),
    ]
    await _run_ffmpeg(args, timeout_sec=120)
    return output_path


async def render_vertical_from_photos(
    photo_paths: Sequence[Path],
    overlay_path: Path,
    output_path: Path,
    work_dir: Path,
) -> Path:
    """Рендерить динамічне вертикальне 9:16 відео (Ken Burns zoom) із 1–4 фотографій + оверлей."""
    selected = list(photo_paths)[:4]
    if not selected:
        raise ValueError("Немає фотографій для монтажу")

    # Тривалість кожного слайду: 8 сек для 1 фото, 3.5 сек для кількох фото
    fps = 25
    slide_sec = 8.0 if len(selected) == 1 else 3.5
    frames_per_slide = int(slide_sec * fps)

    clip_paths: list[Path] = []
    for idx, p in enumerate(selected):
        slide_jpg = work_dir / f"slide_{idx}.jpg"
        await asyncio.to_thread(_prepare_photo_slide, p, slide_jpg)

        clip_mp4 = work_dir / f"clip_{idx}.mp4"
        # Плавний ефект наближення (Ken Burns zoom-in до 1.08x)
        vf = (
            f"zoompan=z='min(zoom+0.0006,1.08)':"
            f"x='iw/2-(iw/zoom/2)':y='ih/2-(ih/zoom/2)':"
            f"d={frames_per_slide}:s={VIDEO_W}x{VIDEO_H}:fps={fps}"
        )
        await _run_ffmpeg(
            [
                "-loop", "1",
                "-i", str(slide_jpg),
                "-vf", vf,
                "-t", f"{slide_sec:.2f}",
                "-c:v", "libx264",
                "-preset", "veryfast",
                "-crf", "22",
                "-pix_fmt", "yuv420p",
                str(clip_mp4),
            ],
            timeout_sec=60,
        )
        clip_paths.append(clip_mp4)

    # Якщо кілька слайдів — склеюємо їх через concat demuxer
    if len(clip_paths) == 1:
        base_video = clip_paths[0]
    else:
        concat_list = work_dir / "concat.txt"
        concat_list.write_text(
            "\n".join(f"file '{c.resolve()}'" for c in clip_paths),
            encoding="utf-8",
        )
        base_video = work_dir / "merged_slides.mp4"
        await _run_ffmpeg(
            [
                "-f", "concat",
                "-safe", "0",
                "-i", str(concat_list),
                "-c", "copy",
                str(base_video),
            ],
            timeout_sec=45,
        )

    # Накладаємо фірмовий PNG-оверлей + додаємо тиху аудіодоріжку (щоб TikTok/Reels коректно приймали файл)
    await _run_ffmpeg(
        [
            "-i", str(base_video),
            "-i", str(overlay_path),
            "-f", "lavfi",
            "-i", "anullsrc=channel_layout=stereo:sample_rate=44100",
            "-filter_complex", "[0:v][1:v]overlay=0:0[out]",
            "-map", "[out]",
            "-map", "2:a",
            "-shortest",
            "-c:v", "libx264",
            "-preset", "veryfast",
            "-crf", "23",
            "-pix_fmt", "yuv420p",
            "-c:a", "aac",
            "-b:a", "96k",
            "-movflags", "+faststart",
            str(output_path),
        ],
        timeout_sec=90,
    )
    return output_path
