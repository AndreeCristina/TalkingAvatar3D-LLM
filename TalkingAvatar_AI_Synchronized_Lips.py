import bpy
import math
import time
import json
import base64
import queue
import random
import threading
import subprocess
import urllib.request
import textwrap
import re
import bisect
import wave
import winsound
import tempfile
import os

from bpy.props import StringProperty

# Talking Avatar AI - Blender 5.2
# Ollama + English TTS + multi-viseme visual lip movement + blinking + subtle head motion
MODEL = "qwen3:4b-instruct"
OLLAMA_URL = "http://127.0.0.1:11434/api/chat"
FACE_NAME = "Human_export_copy"
RIG_NAME = "Human.rig_export_copy"
EYE_BONES = ("orbicularis03.L", "orbicularis03.R")
BLINK_ANGLE = math.radians(-23)
BLINK_DURATION = 0.26
HEAD_BONE = 'head'
HEAD_MAX_TILT_DEGREES = 1.6  # Rare, small tilts rather than constant swaying
# Approximate viseme motion; this is NOT phoneme-synchronized lip sync.
VISEME_STRENGTH = 0.68
STATE_KEY = "talking_avatar_english_state"

# On reload, stop previous voice and invalidate its timer.
previous = bpy.app.driver_namespace.get(STATE_KEY)
if isinstance(previous, dict):
    previous["run_id"] = previous.get("run_id", 0) + 1
    previous["mode"] = "idle"
    # Reset mouth weights that an older script might have left active.
    prior_face = bpy.data.objects.get(FACE_NAME)
    if prior_face is not None and prior_face.type == 'MESH' and prior_face.data.shape_keys:
        for key_name in previous.get('mouth_names', []):
            key = prior_face.data.shape_keys.key_blocks.get(key_name)
            if key is not None:
                key.value = 0.0
    # Restore eyelids if a previous blink was interrupted by reloading.
    prior_rig = bpy.data.objects.get(RIG_NAME)
    if prior_rig is not None:
        for eye_name, eye_angle in previous.get('eye_baselines', {}).items():
            eye_bone = prior_rig.pose.bones.get(eye_name)
            if eye_bone is not None and eye_bone.rotation_mode == 'XYZ':
                eye_bone.rotation_euler.x = eye_angle
    # Restore the head left by the previous version before taking a new baseline.
    prior_rig = bpy.data.objects.get(RIG_NAME)
    prior_base = previous.get('head_baseline')
    if prior_rig is not None and prior_base is not None:
        prior_head = prior_rig.pose.bones.get(HEAD_BONE)
        if prior_head is not None and prior_head.rotation_mode == 'XYZ':
            prior_head.rotation_euler.z = prior_base
    proc = previous.get("process")
    if proc is not None and proc.poll() is None:
        proc.terminate()
    # Stop any audio being played by the previous version.
    try:
        winsound.PlaySound(None, 0)
    except RuntimeError:
        pass

state = {
    "process": None,
    "mode": "idle",
    "start": 0.0,
    "run_id": 0,
    "events": queue.Queue(),
    "blink_start": None,
    "next_blink": time.monotonic() + random.uniform(3.0, 6.0),
    "eye_baselines": {},
    "head_baseline": None,
    "head_event_start": None,
    "head_next_event": None,
    "head_event_duration": 0.0,
    "head_event_target": 0.0,
    "mouth_names": [],
    "mouth_frames": [],
    "mouth_end_times": [],
    "mouth_total_duration": 0.0,
    "mouth_last_time": time.monotonic(),
    "audio_duration": 0.0,
    "audio_file": None,
}
bpy.app.driver_namespace[STATE_KEY] = state


def face_object():
    obj = bpy.data.objects.get(FACE_NAME)
    if obj is None or obj.type != 'MESH' or obj.data.shape_keys is None:
        return None
    if 'viseme_aa' not in obj.data.shape_keys.key_blocks:
        return None
    return obj


def rig_object():
    obj = bpy.data.objects.get(RIG_NAME)
    return obj if obj is not None and obj.type == 'ARMATURE' else None


def redraw():
    for window in bpy.context.window_manager.windows:
        for area in window.screen.areas:
            if area.type == 'VIEW_3D':
                area.tag_redraw()


# Meta/MPFB visemes are detected at runtime (some avatars have fewer).
VISEME_CANDIDATES = (
    'viseme_PP', 'viseme_FF', 'viseme_TH', 'viseme_DD',
    'viseme_kk', 'viseme_CH', 'viseme_SS', 'viseme_nn',
    'viseme_RR', 'viseme_aa', 'viseme_E', 'viseme_I',
    'viseme_O', 'viseme_U',
)


def prepare_mouth():
    obj = face_object()
    if obj is None:
        print('LIPS: Face not found')
        return
    blocks = obj.data.shape_keys.key_blocks
    state['mouth_names'] = [name for name in VISEME_CANDIDATES if name in blocks]
    print('LIPS: Available visemes:', state['mouth_names'])
    reset_mouth()


def reset_mouth():
    obj = face_object()
    if obj is None:
        return
    keys = obj.data.shape_keys.key_blocks
    for name in state['mouth_names']:
        if name in keys:
            keys[name].value = 0.0
    state['mouth_frames'] = []
    state['mouth_end_times'] = []
    state['mouth_total_duration'] = 0.0
    redraw()


def viseme_for_letters(text):
    # Visual approximation of English spelling, not an acoustic analysis.
    t = text.lower()
    if t.startswith(('sh', 'ch', 'j')):
        return 'viseme_CH'
    if t.startswith('th'):
        return 'viseme_TH'
    c = t[0]
    return {
        'p': 'viseme_PP', 'b': 'viseme_PP', 'm': 'viseme_PP',
        'f': 'viseme_FF', 'v': 'viseme_FF',
        't': 'viseme_DD', 'd': 'viseme_DD',
        'n': 'viseme_nn', 'l': 'viseme_nn',
        's': 'viseme_SS', 'z': 'viseme_SS',
        'k': 'viseme_kk', 'g': 'viseme_kk',
        'c': 'viseme_CH', 'r': 'viseme_RR',
        'a': 'viseme_aa', 'e': 'viseme_E',
        'i': 'viseme_I', 'y': 'viseme_I',
        'o': 'viseme_O', 'u': 'viseme_U',
        'w': 'viseme_U', 'q': 'viseme_U',
        'h': 'viseme_aa',
    }.get(c, 'viseme_aa')


def make_mouth_timeline(text):
    # Each small chunk approximates a visible mouth pose in sequence.
    # This timeline is independent of the TTS engine's true phoneme times.
    parts = re.findall(r"[A-Za-z]+|[.,!?;:]", text)
    frames = []
    ends = []
    clock = 0.0
    for part in parts:
        if len(part) == 1 and part in '.,!?;:':
            clock += 0.18 if part in ',;:' else 0.28
            frames.append(None)
            ends.append(clock)
            continue
        chunks = re.findall(r'th|sh|ch|[aeiouy]+|[^aeiouy]', part.lower())
        # Limit unusually long/short token cadence.
        for chunk in chunks:
            visual = viseme_for_letters(chunk)
            duration = min(0.21, max(0.075, len(chunk) * 0.085))
            clock += duration
            frames.append(visual)
            ends.append(clock)
        clock += 0.065
        frames.append(None)
        ends.append(clock)
    state['mouth_frames'] = frames
    state['mouth_end_times'] = ends
    state['mouth_total_duration'] = clock


def update_mouth(now):
    obj = face_object()
    if obj is None:
        return
    blocks = obj.data.shape_keys.key_blocks
    names = state['mouth_names']
    frames = state['mouth_frames']
    ends = state['mouth_end_times']
    if not frames or not ends:
        return

    elapsed = max(0.0, now - state['start'])
    # Fit the estimated mouth sequence to the ACTUAL WAV duration.
    total = state['mouth_total_duration']
    if total <= 0.0:
        return
    audio_duration = state['audio_duration']
    if audio_duration <= 0.0 or elapsed >= audio_duration:
        reset_mouth()
        return
    # The full sequence ends exactly when the WAV file ends; no looping.
    local_t = min(total, elapsed * total / audio_duration)
    index = min(bisect.bisect_left(ends, local_t), len(frames)-1)
    target_name = frames[index]
    # Pause a bit on the first ~0.15 sec while PowerShell initializes.
    if elapsed < 0.15:
        target_name = None
    # Smooth morph changes to avoid twitchy mouth shapes.
    delta = min(0.15, max(0.01, now - state['mouth_last_time']))
    alpha = 1.0 - math.exp(-delta * 19.0)
    state['mouth_last_time'] = now
    for name in names:
        if name not in blocks:
            continue
        target = VISEME_STRENGTH if name == target_name else 0.0
        # Consonants are smaller articulations; open vowels are stronger.
        if target_name and target_name not in ('viseme_aa','viseme_O','viseme_U','viseme_E','viseme_I'):
            if name == target_name:
                target *= 0.75
        key = blocks[name]
        key.value += (target - key.value) * alpha
    redraw()


def prepare_blink():
    rig = rig_object()
    if rig is None:
        print('BLINK: Rig not found')
        return
    for name in EYE_BONES:
        bone = rig.pose.bones.get(name)
        if bone is None:
            print('BLINK: Missing bone:', name)
            continue
        # The eyes were tested in Euler XYZ mode. Preserve the current open-eye angle.
        if bone.rotation_mode != 'XYZ':
            print('BLINK: Expected XYZ rotation for', name, '- currently:', bone.rotation_mode)
            continue
        state['eye_baselines'][name] = bone.rotation_euler.x
    print('BLINK: Ready for', list(state['eye_baselines']))


def prepare_head():
    rig = rig_object()
    if rig is None:
        print('HEAD: Rig not found')
        return
    bone = rig.pose.bones.get(HEAD_BONE)
    if bone is None:
        print('HEAD: Bone not found:', HEAD_BONE)
        return
    # Earlier manual test established Euler XYZ and local Z as the useful tilt.
    if bone.rotation_mode != 'XYZ':
        print('HEAD: Expected XYZ rotation; found', bone.rotation_mode)
        return
    state['head_baseline'] = bone.rotation_euler.z
    print('HEAD: Ready, baseline Z:', state['head_baseline'])


def head_tilt(degrees):
    rig = rig_object()
    if rig is None or state['head_baseline'] is None:
        return
    bone = rig.pose.bones.get(HEAD_BONE)
    if bone is None:
        return
    bone.rotation_euler.z = state['head_baseline'] + math.radians(degrees)
    redraw()


def update_head(now):
    # No movement while waiting for the AI or when the avatar is silent.
    if state['mode'] != 'speaking':
        state['head_event_start'] = None
        state['head_next_event'] = None
        head_tilt(0.0)
        return

    # First gesture happens after a pause, not as soon as speaking starts.
    if state['head_next_event'] is None:
        state['head_next_event'] = now + random.uniform(1.2, 2.8)

    start = state['head_event_start']
    if start is None:
        if now < state['head_next_event']:
            head_tilt(0.0)
            return
        # Each small tilt gets a different direction, amplitude and duration.
        state['head_event_start'] = now
        state['head_event_duration'] = random.uniform(0.75, 1.35)
        state['head_event_target'] = random.choice((-1, 1)) * random.uniform(
            0.7, HEAD_MAX_TILT_DEGREES
        )
        start = now

    progress = (now - start) / state['head_event_duration']
    if progress >= 1.0:
        head_tilt(0.0)
        state['head_event_start'] = None
        # Long motionless pauses are what makes the gestures less robotic.
        state['head_next_event'] = now + random.uniform(2.0, 5.0)
        return

    # Smoothly tip the head and bring it back; don't oscillate continuously.
    if progress <= 0.5:
        weight = smoothstep(progress * 2.0)
    else:
        weight = smoothstep((1.0 - progress) * 2.0)
    head_tilt(state['head_event_target'] * weight)


def blink(amount):
    rig = rig_object()
    if rig is None:
        return
    for name, base in state['eye_baselines'].items():
        bone = rig.pose.bones.get(name)
        if bone is not None:
            bone.rotation_euler.x = base + BLINK_ANGLE * amount
    redraw()


def smoothstep(x):
    x = max(0.0, min(1.0, x))
    return x * x * (3 - 2 * x)


def update_blink(now):
    if len(state['eye_baselines']) != 2:
        return
    if state['blink_start'] is None:
        if now < state['next_blink']:
            return
        state['blink_start'] = now

    elapsed = now - state['blink_start']
    if elapsed >= BLINK_DURATION:
        blink(0.0)
        state['blink_start'] = None
        state['next_blink'] = now + random.uniform(3.0, 6.0)
        return

    half = BLINK_DURATION / 2.0
    phase = elapsed / half if elapsed < half else (BLINK_DURATION - elapsed) / half
    blink(smoothstep(phase))


def clean_audio_file():
    path = state.get('audio_file')
    state['audio_file'] = None
    if path:
        try:
            os.remove(path)
        except OSError:
            pass


def stop_speech():
    state['run_id'] += 1
    state['mode'] = 'idle'
    proc = state['process']
    if proc is not None and proc.poll() is None:
        proc.terminate()
    state['process'] = None
    winsound.PlaySound(None, 0)
    clean_audio_file()
    state['audio_duration'] = 0.0
    reset_mouth()
    head_tilt(0.0)
    state['head_event_start'] = None
    state['head_next_event'] = None


def synthesize_audio_worker(text, run_id):
    # Worker thread: no Blender API calls here.
    fd, path = tempfile.mkstemp(prefix='ava_tts_', suffix='.wav')
    os.close(fd)
    encoded_text = base64.b64encode(text.encode('utf-8')).decode('ascii')
    encoded_path = base64.b64encode(path.encode('utf-8')).decode('ascii')
    ps = f'''
$ErrorActionPreference = 'Stop'
Add-Type -AssemblyName System.Speech
$speaker = New-Object System.Speech.Synthesis.SpeechSynthesizer
$text = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{encoded_text}'))
$wav = [System.Text.Encoding]::UTF8.GetString([System.Convert]::FromBase64String('{encoded_path}'))
try {{
    $voices = @($speaker.GetInstalledVoices() | Where-Object {{ $_.Enabled }})
    $selected = $voices | Where-Object {{ $_.VoiceInfo.Culture.Name -like 'en-*' -and $_.VoiceInfo.Gender.ToString() -eq 'Female' }} | Select-Object -First 1
    if (-not $selected) {{
        $selected = $voices | Where-Object {{ $_.VoiceInfo.Culture.Name -like 'en-*' }} | Select-Object -First 1
    }}
    if ($selected) {{ $speaker.SelectVoice($selected.VoiceInfo.Name) }}
    $speaker.Rate = -1
    $speaker.Volume = 100
    $speaker.SetOutputToWaveFile($wav)
    $speaker.Speak($text)
    $speaker.SetOutputToNull()
}} finally {{ $speaker.Dispose() }}
'''
    encoded_ps = base64.b64encode(ps.encode('utf-16le')).decode('ascii')
    try:
        proc = subprocess.Popen(
            ['powershell.exe', '-NoProfile', '-NonInteractive', '-EncodedCommand', encoded_ps],
            stdout=subprocess.PIPE, stderr=subprocess.PIPE,
            creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0),
        )
        # A newer request may cancel this one; do not interfere with Blender.
        if run_id != state['run_id']:
            proc.terminate()
            raise RuntimeError('Cancelled')
        state['process'] = proc
        _, stderr = proc.communicate(timeout=120)
        if proc.returncode != 0:
            raise RuntimeError('Windows TTS: ' + stderr.decode('utf-8', errors='replace')[-500:])
        with wave.open(path, 'rb') as wav:
            duration = wav.getnframes() / wav.getframerate()
        if duration <= 0:
            raise RuntimeError('Generated WAV is empty')
        if run_id != state['run_id']:
            raise RuntimeError('Cancelled')
        state['events'].put((run_id, 'audio_ready', (path, duration, text)))
    except Exception as exc:
        try:
            os.remove(path)
        except OSError:
            pass
        if run_id == state['run_id']:
            state['events'].put((run_id, 'audio_error', str(exc)))


def speak(text):
    # Preparing a WAV avoids starting lip movement before the actual audio.
    state['mode'] = 'preparing_audio'
    state['process'] = None
    bpy.context.scene.ta_status = 'Preparing voice...'
    req_id = state['run_id']
    threading.Thread(
        target=synthesize_audio_worker, args=(text, req_id), daemon=True
    ).start()


def play_prepared_audio(path, duration, text):
    try:
        winsound.PlaySound(path, winsound.SND_FILENAME | winsound.SND_ASYNC | winsound.SND_NODEFAULT)
        state['audio_file'] = path
        state['audio_duration'] = duration
        state['start'] = time.monotonic()
        state['mode'] = 'speaking'
        state['process'] = None
        state['mouth_last_time'] = state['start']
        make_mouth_timeline(text)
        state['head_event_start'] = None
        state['head_next_event'] = None
        bpy.context.scene.ta_status = 'Speaking...'
        print('AVATAR:', text, '| Audio length:', round(duration, 2), 'sec')
    except Exception as exc:
        state['mode'] = 'idle'
        bpy.context.scene.ta_status = 'Audio playback error'
        print('PLAYBACK ERROR:', exc)
        try:
            os.remove(path)
        except OSError:
            pass


def ask_ollama(question, run_id):
    # Background worker: do not call any bpy API here.
    try:
        payload = {
            'model': MODEL,
            'stream': False,
            'think': False,
            'messages': [
                {'role': 'system', 'content': (
                    'You are Ava, a friendly female 3D avatar created in Blender. '
                    'Always answer in English. Respond directly in one to three short '
                    'sentences. Do not include reasoning, instructions, or stage directions. '
                    'Write only the words that should be spoken.'
                )},
                {'role': 'user', 'content': question},
            ],
            'options': {'temperature': 0.5, 'num_predict': 150},
        }
        request = urllib.request.Request(
            OLLAMA_URL,
            data=json.dumps(payload, ensure_ascii=False).encode('utf-8'),
            headers={'Content-Type': 'application/json'},
            method='POST',
        )
        with urllib.request.urlopen(request, timeout=180) as resp:
            result = json.loads(resp.read().decode('utf-8'))
        answer = result.get('message', {}).get('content', '').strip()
        if not answer:
            raise RuntimeError('Ollama returned an empty final response.')
        state['events'].put((run_id, 'answer', answer))
    except Exception as exc:
        state['events'].put((run_id, 'error', str(exc)))


def timer_update():
    # The old timer exits when this script is run again.
    if bpy.app.driver_namespace.get(STATE_KEY) is not state:
        return None

    now = time.monotonic()
    update_blink(now)
    scene = bpy.context.scene

    if state['mode'] in ('thinking', 'preparing_audio'):
        while True:
            try:
                req_id, kind, content = state['events'].get_nowait()
            except queue.Empty:
                break
            if req_id != state['run_id']:
                # A cancelled WAV is no longer used; clean it up.
                if kind == 'audio_ready':
                    try:
                        os.remove(content[0])
                    except OSError:
                        pass
                continue
            if kind in ('error', 'audio_error'):
                state['mode'] = 'idle'
                scene.ta_status = 'AI error' if kind == 'error' else 'Windows voice error'
                scene.ta_answer = content if kind == 'error' else scene.ta_answer
                print('ERROR:', content)
            elif kind == 'answer':
                scene.ta_answer = content
                speak(content)
            elif kind == 'audio_ready':
                path, duration, text = content
                play_prepared_audio(path, duration, text)
            break

    if state['mode'] == 'speaking':
        # WAV duration is measured from its frame count; no guesswork.
        if now - state['start'] >= state['audio_duration']:
            winsound.PlaySound(None, 0)
            clean_audio_file()
            state['mode'] = 'idle'
            scene.ta_status = 'Finished'
            reset_mouth()
        else:
            update_mouth(now)

    update_head(now)
    return 0.04


def prepare_scene(context):
    if context.screen is not None and context.screen.is_animation_playing:
        bpy.ops.screen.animation_cancel(restore_frame=False)
    context.scene.frame_set(1)
    reset_mouth()


class TALKINGAVATAR_OT_ask(bpy.types.Operator):
    bl_idname = 'talking_avatar.ask'
    bl_label = 'Ask AI'

    def execute(self, context):
        question = context.scene.avatar_text.strip()
        if not question:
            self.report({'WARNING'}, 'Enter a question.')
            return {'CANCELLED'}
        if face_object() is None:
            self.report({'ERROR'}, 'Human_export_copy with viseme_aa not found.')
            return {'CANCELLED'}
        stop_speech()
        prepare_scene(context)
        context.scene.ta_answer = ''
        context.scene.ta_status = 'Generating AI answer...'
        state['mode'] = 'thinking'
        req_id = state['run_id']
        threading.Thread(target=ask_ollama, args=(question, req_id), daemon=True).start()
        return {'FINISHED'}


class TALKINGAVATAR_OT_speak(bpy.types.Operator):
    bl_idname = 'talking_avatar.speak'
    bl_label = 'Speak Without AI'

    def execute(self, context):
        text = context.scene.avatar_text.strip()
        if not text:
            self.report({'WARNING'}, 'Enter text.')
            return {'CANCELLED'}
        if face_object() is None:
            self.report({'ERROR'}, 'Human_export_copy with viseme_aa not found.')
            return {'CANCELLED'}
        stop_speech()
        prepare_scene(context)
        speak(text)
        return {'FINISHED'}


class TALKINGAVATAR_OT_stop(bpy.types.Operator):
    bl_idname = 'talking_avatar.stop'
    bl_label = 'Stop Speaking'

    def execute(self, context):
        stop_speech()
        context.scene.ta_status = 'Stopped (blinking remains active)'
        return {'FINISHED'}


class TALKINGAVATAR_PT_panel(bpy.types.Panel):
    bl_label = 'Talking Avatar AI'
    bl_idname = 'TALKINGAVATAR_PT_panel'
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Talking Avatar'

    def draw(self, context):
        layout = self.layout
        scene = context.scene
        layout.label(text='Your question or text:')
        layout.prop(scene, 'avatar_text', text='')
        layout.separator()
        layout.operator('talking_avatar.ask', text='Ask AI', icon='PLAY')
        layout.operator('talking_avatar.speak', text='Speak Without AI')
        layout.operator('talking_avatar.stop', text='Stop Speaking', icon='CANCEL')
        layout.separator()
        layout.label(text='Model: ' + MODEL)
        layout.label(text='Lips: fit to measured audio length')
        layout.label(text='Blink: -23 degrees, every 3-6 seconds')
        layout.label(text='Head: occasional gentle tilts (with pauses)')
        layout.label(text='Status: ' + scene.ta_status)
        if scene.ta_answer:
            layout.separator()
            layout.label(text='AI Answer:')
            for line in textwrap.wrap(scene.ta_answer, width=34)[:15]:
                layout.label(text=line)


classes = (
    TALKINGAVATAR_OT_ask,
    TALKINGAVATAR_OT_speak,
    TALKINGAVATAR_OT_stop,
    TALKINGAVATAR_PT_panel,
)

# Unregister previous versions of these controls, allowing repeated Run Script.
for cls in reversed(classes):
    base = bpy.types.Panel if issubclass(cls, bpy.types.Panel) else bpy.types.Operator
    old = base.bl_rna_get_subclass_py(cls.__name__, None)
    if old is not None:
        try:
            bpy.utils.unregister_class(old)
        except RuntimeError:
            pass

if not hasattr(bpy.types.Scene, 'avatar_text'):
    bpy.types.Scene.avatar_text = StringProperty(name='Question', default='Hello Ava! Can you introduce yourself?')
if not hasattr(bpy.types.Scene, 'ta_answer'):
    bpy.types.Scene.ta_answer = StringProperty(name='AI Answer', default='')
if not hasattr(bpy.types.Scene, 'ta_status'):
    bpy.types.Scene.ta_status = StringProperty(name='Status', default='Ready')

for cls in classes:
    bpy.utils.register_class(cls)

prepare_blink()
prepare_head()
prepare_mouth()
bpy.context.scene.ta_status = 'Ready - blinking and natural head gestures active'
bpy.app.timers.register(timer_update, first_interval=0.04)
print('Talking Avatar AI: audio-timed visemes, Ollama, -23 degree blinking, natural head gestures.')
