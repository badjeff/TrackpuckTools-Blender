# TrackpuckTools Blender Add-in
# 6DoF HID Peripheral Navigation for Blender Viewport

bl_info = {
    "name": "TrackpuckTools",
    "author": "badjeff",
    "version": (1, 0, 0),
    "blender": (4, 0, 0),
    "location": "View3D > TrackpuckTools",
    "description": "Trackpuck 6DoF Navigation Addin",
    "category": "3D View",
    "doc_url": "https://github.com/badjeff/TrackpuckTools-Blender",
}

import os
import sys
import traceback
import json
import struct
import threading
import queue
import math
import mathutils # pyright: ignore[reportMissingImports]
import glob
import zipfile
import importlib
import importlib.util
import bpy # pyright: ignore[reportMissingImports]
from bpy.props import FloatProperty, EnumProperty, BoolProperty, PointerProperty # pyright: ignore[reportMissingImports]
from bpy.types import Operator, Panel, PropertyGroup # pyright: ignore[reportMissingImports]


addon_dir = os.path.dirname(os.path.abspath(__file__))
config_path = os.path.join(addon_dir, "config.json")
prefs_path = os.path.join(addon_dir, "prefs.json")

DEBUG_LOG = False

def log(msg):
    if DEBUG_LOG:
        try:
            import datetime
            log_path = os.path.join(addon_dir, "trackpuck_debug.log")
            with open(log_path, "a") as f:
                f.write(f"{datetime.datetime.now()}: {msg}\n")
        except:
            pass
        print(f"[TrackpuckTools] {msg}")

log("TrackpuckTools starting...")

# -------------------------
# Config
# -------------------------
REQUIRED_PACKAGES = [
    ("hid", "hidapi"),
]

def parse_hex_or_int(value, default):
    if value is None:
        return default
    try:
        if isinstance(value, str):
            return int(value, 0)
        return int(value)
    except (ValueError, TypeError):
        return None

try:
    with open(config_path, 'r') as f:
        _default_config = json.load(f)
    log(f"Loaded config: {_default_config}")
except Exception as e:
    log(f"Failed to load config: {e}")
    _default_config = {}

VENDOR_ID_STR = _default_config.get('VENDOR_ID')
PRODUCT_ID_STR = _default_config.get('PRODUCT_ID')

VENDOR_ID = parse_hex_or_int(VENDOR_ID_STR, 0x1d50)
PRODUCT_ID = parse_hex_or_int(PRODUCT_ID_STR, 0x615e)

config_valid = VENDOR_ID is not None and PRODUCT_ID is not None
config_error = None
if not config_valid:
    config_error = f"Invalid VENDOR_ID or PRODUCT_ID in config.json"
    log(config_error)

log(f"Config: VID={hex(VENDOR_ID) if VENDOR_ID else 'None'}, PID={hex(PRODUCT_ID) if PRODUCT_ID else 'None'}")

ORTHO_MODE_ZOOM_FACTOR_COEFF = 0.1289
PROCESS_EVENT_SLEEP_SEC = 0.01

hid = None
device = None
running = False
hid_queue = None
hid_queue_stop_event = None
hid_thread = None
device_connected = False
disconnect_error = None
timer_handle = None
libs_ready = False
import_error = None
ui_redraw_counter = 0


# -------------------------
# Preferences
# -------------------------
def save_prefs(prefs):
    try:
        if prefs_path:
            with open(prefs_path, "w") as f:
                json.dump(prefs, f)
            log(f"Saved prefs: {prefs}")
    except Exception as e:
        log(f"Failed to save prefs: {e}")


def load_prefs():
    try:
        if prefs_path and os.path.exists(prefs_path):
            with open(prefs_path, "r") as f:
                prefs = json.load(f)
                log(f"Loaded prefs: {prefs}")
                return prefs
    except Exception as e:
        log(f"Failed to load prefs: {e}")
    return {}


def apply_prefs_to_props(props, prefs):
    props.motion_mode = str(prefs.get('MOTION_MODE', _default_config.get('MOTION_MODE', 1)))
    props.near_distance = prefs.get('NEAR_DISTANCE', _default_config.get('NEAR_DISTANCE', 1.0))
    props.far_distance = prefs.get('FAR_DISTANCE', _default_config.get('FAR_DISTANCE', 100.0))
    props.near_trans_sensitivity = prefs.get('NEAR_TRANS_SENSITIVITY', _default_config.get('NEAR_TRANS_SENSITIVITY', 0.1))
    props.far_trans_sensitivity = prefs.get('FAR_TRANS_SENSITIVITY', _default_config.get('FAR_TRANS_SENSITIVITY', 1.77))
    props.rotation_sensitivity = prefs.get('ROTATION_SENSITIVITY', _default_config.get('ROTATION_SENSITIVITY', 0.12))
    props.scale_x = prefs.get('SCALE_X', _default_config.get('SCALE_X', 1.0))
    props.scale_y = prefs.get('SCALE_Y', _default_config.get('SCALE_Y', 1.0))
    props.scale_z = prefs.get('SCALE_Z', _default_config.get('SCALE_Z', 1.0))
    props.scale_rx = prefs.get('SCALE_RX', _default_config.get('SCALE_RX', 1.0))
    props.scale_ry = prefs.get('SCALE_RY', _default_config.get('SCALE_RY', 1.0))
    props.scale_rz = prefs.get('SCALE_RZ', _default_config.get('SCALE_RZ', 1.0))


def reset_all_to_defaults(props):
    props.motion_mode = str(_default_config.get('MOTION_MODE', 1))
    props.near_distance = _default_config.get('NEAR_DISTANCE', 1.0)
    props.far_distance = _default_config.get('FAR_DISTANCE', 100.0)
    props.near_trans_sensitivity = _default_config.get('NEAR_TRANS_SENSITIVITY', 0.1)
    props.far_trans_sensitivity = _default_config.get('FAR_TRANS_SENSITIVITY', 1.77)
    props.rotation_sensitivity = _default_config.get('ROTATION_SENSITIVITY', 0.12)
    props.scale_x = _default_config.get('SCALE_X', 1.0)
    props.scale_y = _default_config.get('SCALE_Y', 1.0)
    props.scale_z = _default_config.get('SCALE_Z', 1.0)
    props.scale_rx = _default_config.get('SCALE_RX', 1.0)
    props.scale_ry = _default_config.get('SCALE_RY', 1.0)
    props.scale_rz = _default_config.get('SCALE_RZ', 1.0)


# -------------------------
# Import modules
# -------------------------
def find_wheel_file(pkg_name):
    script_dir = os.path.dirname(os.path.abspath(__file__))
    pattern = os.path.join(script_dir, pkg_name, "*.whl")
    wheels = glob.glob(pattern)
    
    for wheel in wheels:
        wheel_lower = os.path.basename(wheel).lower()
        if pkg_name.lower() in wheel_lower:
            return wheel
    
    return None


def extract_wheel(wheel_path, extract_dir):
    if not os.path.exists(extract_dir):
        os.makedirs(extract_dir)
    
    with zipfile.ZipFile(wheel_path, 'r') as zip_ref:
        zip_ref.extractall(extract_dir)


def get_module_paths(extract_dir):
    paths = [extract_dir]
    
    for item in os.listdir(extract_dir):
        full_path = os.path.join(extract_dir, item)
        if os.path.isdir(full_path) and item.endswith('.dist-info'):
            continue
        if os.path.isdir(full_path):
            paths.append(full_path)
    
    return paths


def find_module_in_extracted(extract_dir, pkg_name):
    pkg_name_lower = pkg_name.lower()
    
    for item in os.listdir(extract_dir):
        full_path = os.path.join(extract_dir, item)
        item_lower = item.lower()
        
        if item.endswith('.dist-info') or item.endswith('.pth'):
            continue
        
        if os.path.isdir(full_path) and item_lower == pkg_name_lower:
            return full_path, item
        
        if os.path.isfile(full_path) and item_lower == f"{pkg_name_lower}.py":
            return full_path, item.replace('.py', '')
        
        if os.path.isfile(full_path) and item_lower.startswith(f"{pkg_name_lower}."):
            if item.endswith(('.so', '.pyd', '.dll')):
                return full_path, pkg_name
    
    return None, None


def import_libs():
    global hid, import_error
    import_error = None
    lib_ready = True
    report = []
    missing_packages = []
    
    for pkg_spec in REQUIRED_PACKAGES:
        if isinstance(pkg_spec, tuple):
            import_name, package_name = pkg_spec
        else:
            import_name = package_name = pkg_spec
        
        try:
            module = importlib.import_module(import_name)
            if hasattr(module, 'device'):
                globals()[import_name] = module
                report.append(f"{import_name}: OK (has device)")
                log(f"{import_name}: OK")
            else:
                raise ImportError(f"Module {import_name} imported but missing 'device' attribute")
        except ImportError:
            wheel_path = find_wheel_file(package_name)
            
            if wheel_path:
                try:
                    script_dir = os.path.dirname(os.path.abspath(__file__))
                    extract_dir = os.path.join(script_dir, "__extracted__", package_name)
                    
                    log(f"Found wheel at {wheel_path}")
                    extract_wheel(wheel_path, extract_dir)
                    
                    module_paths = get_module_paths(extract_dir)
                    for path in module_paths:
                        if path not in sys.path:
                            sys.path.insert(0, path)
                    
                    module_path, module_name = find_module_in_extracted(extract_dir, import_name)
                    
                    if module_path:
                        parent_dir = os.path.dirname(module_path) if os.path.isfile(module_path) else module_path
                        if parent_dir not in sys.path:
                            sys.path.insert(0, parent_dir)
                        
                        if os.path.isfile(module_path):
                            spec = importlib.util.spec_from_file_location(import_name, module_path)
                            if spec and spec.loader:
                                module = importlib.util.module_from_spec(spec)
                                sys.modules[import_name] = module
                                spec.loader.exec_module(module)
                            else:
                                raise ImportError(f"Cannot load spec from {module_path}")
                        else:
                            init_path = os.path.join(module_path, "__init__.py")
                            if os.path.exists(init_path):
                                spec = importlib.util.spec_from_file_location(import_name, init_path)
                                if spec and spec.loader:
                                    module = importlib.util.module_from_spec(spec)
                                    sys.modules[import_name] = module
                                    spec.loader.exec_module(module)
                                else:
                                    raise ImportError(f"Cannot load spec from {init_path}")
                            else:
                                module = importlib.import_module(import_name)
                        
                        globals()[import_name] = module
                        if not hasattr(module, 'device'):
                            raise ImportError(f"Module {import_name} loaded but missing 'device' attribute")
                        log(f"{import_name}: Loaded from wheel")
                    else:
                        module = importlib.import_module(import_name)
                        globals()[import_name] = module
                        log(f"{import_name}: Loaded")
                    
                except Exception as e:
                    report.append(f"{import_name}: Error - {str(e)}")
                    log(f"{import_name}: Error - {e}")
                    missing_packages.append(package_name)
                    lib_ready = False
            else:
                missing_packages.append(package_name)
                lib_ready = False
    
    if missing_packages:
        import_error = f"Missing: {', '.join(missing_packages)}"
        log(import_error)
    
    return lib_ready


def pull_libs():
    import urllib.request
    
    script_dir = os.path.dirname(os.path.abspath(__file__))
    packages_to_pull = ["hidapi"]
    
    for pkg_name in packages_to_pull:
        pkg_dir = os.path.join(script_dir, pkg_name)
        if not os.path.exists(pkg_dir):
            os.makedirs(pkg_dir)
        
        wheel_path = find_wheel_file(pkg_name)
        if wheel_path:
            log(f"pull_libs: wheel already exists: {wheel_path}")
            continue
        
        try:
            log(f"pull_libs: downloading {pkg_name} from PyPI...")
            
            metadata_url = f"https://pypi.org/pypi/{pkg_name}/json"
            with urllib.request.urlopen(metadata_url, timeout=30) as response:
                metadata = json.loads(response.read().decode())
            
            releases = metadata.get("releases", {})
            if not releases:
                log(f"pull_libs: no releases found for {pkg_name}")
                continue
            
            version = metadata["info"]["version"]
            log(f"pull_libs: latest version is {version}")
            
            system = sys.platform
            machine = os.uname().machine
            
            if system == "darwin":
                platform_tag = "macosx"
                if machine == "x86_64":
                    platform_tag += "_10_9_x86_64"
                elif machine == "arm64":
                    platform_tag += "_11_0_arm64"
            elif system == "win32":
                platform_tag = "win_amd64"
            else:
                platform_tag = "manylinux2014_x86_64"
            
            py_version = f"cp{sys.version_info.major}{sys.version_info.minor}"
            
            log(f"pull_libs: looking for {py_version} on {platform_tag}")

            best_wheel_url = None
            best_wheel_filename = None
            
            for rel_version, files in releases.items():
                for file_info in files:
                    filename = file_info["filename"]
                    if filename.endswith(".whl"):
                        if f"-{py_version}-{py_version}-" in filename:
                            log(f"pull_libs: {filename}")
                        if f"-{py_version}-{py_version}-" in filename and platform_tag in filename:
                            best_wheel_url = file_info["url"]
                            best_wheel_filename = filename
                            break
                if best_wheel_url:
                    break
            
            if not best_wheel_url:
                for rel_version, files in releases.items():
                    for file_info in files:
                        filename = file_info["filename"]
                        if filename.endswith(".whl") and py_version in filename:
                            best_wheel_url = file_info["url"]
                            best_wheel_filename = filename
                            break
                if best_wheel_url:
                    break
            
            if not best_wheel_url:
                log(f"pull_libs: no compatible wheel found for {pkg_name}")
                continue
            
            log(f"pull_libs: downloading {best_wheel_filename}...")
            dest_path = os.path.join(pkg_dir, best_wheel_filename)
            
            with urllib.request.urlopen(best_wheel_url, timeout=60) as response:
                with open(dest_path, "wb") as f:
                    f.write(response.read())
            
            log(f"pull_libs: downloaded {best_wheel_filename}")
            
        except Exception as e:
            log(f"pull_libs: error downloading {pkg_name}: {str(e)}")
            continue
    
    return True


# -------------------------
# Properties
# -------------------------
class TrackpuckProperties(PropertyGroup):
    motion_mode: EnumProperty(
        name="Motion Mode",
        items=[
            ('1', "Orbital", "Rotate camera around a fixed target point"),
            ('2', "Navigating", "Move camera directly through 3D space"),
        ],
        default=str(_default_config.get('MOTION_MODE', 1)),
    ) # pyright: ignore[reportInvalidTypeForm]

    near_distance: FloatProperty(
        name="Near Distance",
        default=_default_config.get('NEAR_DISTANCE', 1.0),
        min=0.1,
        max=1000.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    far_distance: FloatProperty(
        name="Far Distance",
        default=_default_config.get('FAR_DISTANCE', 100.0),
        min=0.1,
        max=1000.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    near_trans_sensitivity: FloatProperty(
        name="Near Trans",
        default=_default_config.get('NEAR_TRANS_SENSITIVITY', 0.1),
        min=0.001,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    far_trans_sensitivity: FloatProperty(
        name="Far Trans",
        default=_default_config.get('FAR_TRANS_SENSITIVITY', 1.33),
        min=0.001,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    rotation_sensitivity: FloatProperty(
        name="Rotation",
        default=_default_config.get('ROTATION_SENSITIVITY', 0.123),
        min=0.001,
        max=1.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    scale_x: FloatProperty(
        name="X",
        default=_default_config.get('SCALE_X', 1.0),
        min=-10.0,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    scale_y: FloatProperty(
        name="Y",
        default=_default_config.get('SCALE_Y', 1.0),
        min=-10.0,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    scale_z: FloatProperty(
        name="Z",
        default=_default_config.get('SCALE_Z', 1.0),
        min=-10.0,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    scale_rx: FloatProperty(
        name="RX",
        default=_default_config.get('SCALE_RX', 1.0),
        min=-10.0,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    scale_ry: FloatProperty(
        name="RY",
        default=_default_config.get('SCALE_RY', 1.0),
        min=-10.0,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    scale_rz: FloatProperty(
        name="RZ",
        default=_default_config.get('SCALE_RZ', 1.0),
        min=-10.0,
        max=10.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    dynamic_trans_sensitivity: FloatProperty(
        name="Dynamic Trans",
        default=0.0,
    ) # pyright: ignore[reportInvalidTypeForm]

    connected: BoolProperty(
        name="Connected",
        description="Device connection status",
        default=False,
    ) # pyright: ignore[reportInvalidTypeForm]

    show_motion_mode: BoolProperty(
        name="Motion Mode",
        default=True
    ) # pyright: ignore[reportInvalidTypeForm]

    show_sensitivity: BoolProperty(
        name="Sensitivity",
        default=True
    ) # pyright: ignore[reportInvalidTypeForm]

    show_axis_scales: BoolProperty(
        name="Axis Scales",
        default=True
    ) # pyright: ignore[reportInvalidTypeForm]

    show_preferences: BoolProperty(
        name="Preferences",
        default=True
    ) # pyright: ignore[reportInvalidTypeForm]


# -------------------------
# Panel
# -------------------------
class TRACKPUCK_PT_panel(Panel):
    bl_space_type = 'VIEW_3D'
    bl_region_type = 'UI'
    bl_category = 'Trackpuck'
    bl_label = "Trackpuck"

    # ---------- UI HELPERS ----------

    def draw_foldout(self, layout, props, prop_name, label):
        row = layout.row()
        icon = 'TRIA_DOWN' if getattr(props, prop_name) else 'TRIA_RIGHT'
        row.prop(props, prop_name, icon=icon, icon_only=True, emboss=False)
        row.label(text=label)
        return getattr(props, prop_name)

    def draw_labeled_prop(self, layout, props, prop_name, label):
        split = layout.split(factor=0.6)
        col = split.column()
        row = col.row()
        row.alignment = 'RIGHT'
        row.label(text=label)
        col = split.column()
        col.prop(props, prop_name, text="")

    # ---------- PANEL DRAW ----------

    def draw(self, context):
        global config_error, libs_ready, import_error
        layout = self.layout
        props = context.scene.trackpuck

        # Config error
        if config_error:
            box = layout.box()
            box.label(text=config_error, icon='ERROR')
            box.label(text="Please check config.json")
            return

        # Libs status
        if not libs_ready:
            box = layout.box()
            box.label(text="Library not loaded", icon='ERROR')
            box.label(text=import_error or "Unknown error")
            return

        # Connect button
        row = layout.row()
        if props.connected:
            row.operator("trackpuck.toggle", text="Disconnect", icon='X')
        else:
            row.operator("trackpuck.toggle", text="Connect", icon='PLAY')

        layout.separator()

        # Motion Mode
        box = layout.box()
        if self.draw_foldout(box, props, "show_motion_mode", "Motion Mode"):
            row = box.row()
            row.prop(props, "motion_mode", expand=True)

        # Sensitivity
        box = layout.box()
        if self.draw_foldout(box, props, "show_sensitivity", "Sensitivity"):
            self.draw_labeled_prop(box, props, "near_distance", "Near Distance")
            self.draw_labeled_prop(box, props, "near_trans_sensitivity", "Near Sensitivity")
            self.draw_labeled_prop(box, props, "far_distance", "Far Distance")
            self.draw_labeled_prop(box, props, "far_trans_sensitivity", "Far Sensitivity")
            self.draw_labeled_prop(box, props, "rotation_sensitivity", "Rotation")

        # Axis Scales
        box = layout.box()
        if self.draw_foldout(box, props, "show_axis_scales", "Axis Scales"):
            self.draw_labeled_prop(box, props, "scale_x", "X")
            self.draw_labeled_prop(box, props, "scale_y", "Y")
            self.draw_labeled_prop(box, props, "scale_z", "Z")
            self.draw_labeled_prop(box, props, "scale_rx", "RX")
            self.draw_labeled_prop(box, props, "scale_ry", "RY")
            self.draw_labeled_prop(box, props, "scale_rz", "RZ")

        # Preferences
        box = layout.box()
        if self.draw_foldout(box, props, "show_preferences", "Preferences"):

            row = box.row(align=True)
            row.operator("trackpuck.save_prefs", text="Save", icon='FILE_TICK')
            row.separator()
            row.operator("trackpuck.load_prefs", text="Load", icon='FILE_FOLDER')

            row = box.row()
            row.operator("trackpuck.reset_all", text="Reset All", icon='FILE_REFRESH')

# -------------------------
# HID Device
# -------------------------
def hid_loop():
    global device, running, hid_queue, hid_queue_stop_event, disconnect_error

    if not device:
        log("hid_loop: no device")
        return

    try:
        device.set_nonblocking(True)
        log("Device set to non-blocking mode")
    except Exception as e:
        log(f"HID device error: {e}")
        disconnect_error = "Device error"
        return

    while running and not hid_queue_stop_event.is_set():
        try:
            data = device.read(64)
            if data:
                props = bpy.context.scene.trackpuck
                tx = struct.unpack('b', bytes([data[1]]))[0] / 127.0 * props.scale_x
                ty = struct.unpack('b', bytes([data[2]]))[0] / 127.0 * props.scale_y
                tz = struct.unpack('b', bytes([data[3]]))[0] / 127.0 * props.scale_z
                rx = struct.unpack('b', bytes([data[4]]))[0] / 127.0 * props.scale_rx
                ry = struct.unpack('b', bytes([data[5]]))[0] / 127.0 * props.scale_ry
                rz = struct.unpack('b', bytes([data[6]]))[0] / 127.0 * props.scale_rz
                btns = data[7]
                # log(f"queued: {tx}, {ty}, {tz}, {rx}, {ry}, {rz}")
                hid_queue.put((tx, ty, tz, rx, ry, rz, btns))
        except Exception as e:
            log(f"Device read error: {e}")
            disconnect_error = "Device disconnected"
            break

        hid_queue_stop_event.wait(PROCESS_EVENT_SLEEP_SEC)
    
    log("hid_loop exiting")


def activate_device():
    global VENDOR_ID, PRODUCT_ID, hid, device, running, hid_thread
    global hid_queue_stop_event, hid_queue, device_connected

    log(f"activate_device: VID={hex(VENDOR_ID)}, PID={hex(PRODUCT_ID)}")

    try:
        hid_device_path = None

        for dev in hid.enumerate():
            # log(f"Found HID: VID={hex(dev['vendor_id'])}, PID={hex(dev['product_id'])}, usage={dev.get('usage', 0)}")
            if dev['vendor_id'] == VENDOR_ID and dev['product_id'] == PRODUCT_ID:
                if dev.get('usage', 0) == 0x4:
                    try:
                        device = hid.device()
                        device.open_path(dev['path'])
                        hid_device_path = dev['path']
                        log(f"Device opened: {dev.get('product_string', 'Unknown')}")
                    except IOError as e:
                        log(f"Couldn't open device: {e}")
                    break

        if hid_device_path is None:
            log("No Trackpuck found")
            return False

        if not device:
            log("Failed to create device")
            return False

        device_connected = True
        running = True

        hid_queue = queue.Queue()
        hid_queue_stop_event = threading.Event()

        hid_thread = threading.Thread(target=hid_loop)
        hid_thread.daemon = True
        hid_thread.start()
        
        log("Device activated")
        return True

    except Exception as e:
        log(f"Error activating device: {e}")
        device_connected = False
        return False


def deactivate_device():
    global running, device, hid_thread, hid_queue_stop_event, device_connected

    log("deactivate_device: starting...")
    running = False
    device_connected = False

    if hid_queue_stop_event:
        hid_queue_stop_event.set()

    if hid_thread and hid_thread.is_alive():
        hid_thread.join(timeout=2.0)

    if device:
        try:
            device.close()
        except Exception:
            pass
        device = None
    
    try:
        bpy.context.scene.trackpuck.connected = False
    except:
        pass

    log("deactivate_device: done")


# -------------------------
# Motion functions
# -------------------------
def apply_motion(tx, ty, tz, rx, ry, rz):
    
    props = bpy.context.scene.trackpuck

    area = None
    for a in bpy.context.screen.areas:
        if a.type == 'VIEW_3D':
            area = a
            break
    
    if not area:
        return

    space = area.spaces.active
    if not space:
        return

    region3d = space.region_3d
    if not region3d:
        return

    is_ortho = region3d.view_perspective == 'ORTHO'
    
    if is_ortho:
        distance = region3d.view_distance * 0.5
    else:
        distance = region3d.view_distance
    
    near_dist = props.near_distance
    far_dist = props.far_distance
    near_sens = props.near_trans_sensitivity
    far_sens = props.far_trans_sensitivity
    
    if distance <= near_dist:
        dyn_trans_sensitivity = near_sens
    elif distance >= far_dist:
        dyn_trans_sensitivity = far_sens
    else:
        t = (distance - near_dist) / (far_dist - near_dist)
        dyn_trans_sensitivity = near_sens + t * (far_sens - near_sens)
    
    props.dynamic_trans_sensitivity = dyn_trans_sensitivity

    if tx != 0 or tz != 0:
        offset = mathutils.Vector((-tx, tz, 0.0)) * dyn_trans_sensitivity
        region3d.view_location = region3d.view_location + (region3d.view_rotation @ offset)

    if ty != 0:
        if is_ortho:
            zoom_factor = ty * dyn_trans_sensitivity * ORTHO_MODE_ZOOM_FACTOR_COEFF
            new_distance = region3d.view_distance * (1.0 - zoom_factor)
            if new_distance > 0.001:
                region3d.view_distance = new_distance
        else:
            direction = region3d.view_rotation @ mathutils.Vector((0.0, 0.0, -1.0))
            region3d.view_location = region3d.view_location + direction * (ty * dyn_trans_sensitivity)

    if rz != 0:
        angle = rz * props.rotation_sensitivity
        axis = (region3d.view_rotation @ mathutils.Vector((0.0, 1.0, 0.0))).normalized()
        region3d.view_rotation = mathutils.Quaternion(axis, angle) @ region3d.view_rotation

    if rx != 0:
        angle = -rx * props.rotation_sensitivity
        axis = (region3d.view_rotation @ mathutils.Vector((1.0, 0.0, 0.0))).normalized()
        region3d.view_rotation = mathutils.Quaternion(axis, angle) @ region3d.view_rotation

    if ry != 0:
        angle = ry * props.rotation_sensitivity
        axis = (region3d.view_rotation @ mathutils.Vector((0.0, 0.0, 1.0))).normalized()
        region3d.view_rotation = mathutils.Quaternion(axis, angle) @ region3d.view_rotation


# -------------------------
# Timer
# -------------------------
def trackpuck_timer():
    global disconnect_error, device_connected

    if not device_connected:
        return 0.1

    if disconnect_error:
        log(f"Timer: disconnect error - {disconnect_error}")
        disconnect_error = None
        deactivate_device()
        return 0.1

    motion_mode = int(bpy.context.scene.trackpuck.motion_mode)
    
    try:
        while True:
            tx, ty, tz, rx, ry, rz, btns = hid_queue.get_nowait()
            # log(f"dequeued: {btns}, {tx}, {ty}, {tz}, {rx}, {ry}, {rz}")

            try:
                if btns & 1:
                    pass

                if tx == 0 and ty == 0 and tz == 0 and rx == 0 and ry == 0 and rz == 0:
                    continue

                props = bpy.context.scene.trackpuck
                tx = tx * props.scale_x
                ty = ty * props.scale_y
                tz = tz * props.scale_z
                rx = rx * props.scale_rx
                ry = ry * props.scale_ry
                rz = rz * props.scale_rz

                # Navigating mode motion is inversed
                if motion_mode == 2:
                    tx = -tx
                    ty = -ty
                    tz = -tz
                    rx = -rx
                    ry = -ry
                    rz = -rz

                apply_motion(tx, ty, tz, rx, ry, rz)

            except:
                log(f"{ traceback.format_exc() }")
                break

    except queue.Empty:
        pass

    return 0.01


# -------------------------
# Operator
# -------------------------
class OT_TrackpuckToggle(Operator):
    bl_idname = "trackpuck.toggle"
    bl_label = "Connect Trackpuck"
    bl_options = {'REGISTER'}

    def execute(self, context):
        global timer_handle, libs_ready, device_connected
        
        log("Toggle: start")
        
        if not config_valid:
            log("Toggle: config invalid")
            self.report({'ERROR'}, config_error)
            return {'CANCELLED'}
        
        props = context.scene.trackpuck
        
        if props.connected:
            log("Toggle: disconnecting...")
            deactivate_device()
            if timer_handle:
                try:
                    bpy.app.timers.unregister(trackpuck_timer)
                except:
                    pass
                timer_handle = None
            props.connected = False
            log("Toggle: disconnected")
            self.report({'INFO'}, "Trackpuck disconnected")
            if context.area:
                context.area.tag_redraw()
        else:
            log("Toggle: connecting...")
            
            if not libs_ready:
                log("Toggle: importing libs...")
                if not import_libs():
                    props.connected = False
                    log(f"Toggle: import failed - {import_error}")
                    self.report({'ERROR'}, import_error or "Failed to import library")
                    return {'CANCELTED'}
                libs_ready = True
            
            log("Toggle: activating device...")
            if activate_device():
                props.connected = True
                log("Toggle: connected successfully")
                
                if not timer_handle:
                    timer_handle = bpy.app.timers.register(trackpuck_timer)
                
                self.report({'INFO'}, "Trackpuck connected")
            else:
                props.connected = False
                log("Toggle: device not found")
                self.report({'ERROR'}, "No Trackpuck device found")
        
        return {'FINISHED'}


class OT_SavePreferences(Operator):
    bl_idname = "trackpuck.save_prefs"
    bl_label = "Save Preferences"

    def execute(self, context):
        props = context.scene.trackpuck
        prefs_data = {
            'MOTION_MODE': int(props.motion_mode),
            'NEAR_DISTANCE': props.near_distance,
            'FAR_DISTANCE': props.far_distance,
            'NEAR_TRANS_SENSITIVITY': props.near_trans_sensitivity,
            'FAR_TRANS_SENSITIVITY': props.far_trans_sensitivity,
            'ROTATION_SENSITIVITY': props.rotation_sensitivity,
            'SCALE_X': props.scale_x,
            'SCALE_Y': props.scale_y,
            'SCALE_Z': props.scale_z,
            'SCALE_RX': props.scale_rx,
            'SCALE_RY': props.scale_ry,
            'SCALE_RZ': props.scale_rz,
        }
        save_prefs(prefs_data)
        self.report({'INFO'}, "Preferences saved")
        return {'FINISHED'}


class OT_LoadPreferences(Operator):
    bl_idname = "trackpuck.load_prefs"
    bl_label = "Load Preferences"

    def execute(self, context):
        saved = load_prefs()
        if saved:
            apply_prefs_to_props(context.scene.trackpuck, saved)
            self.report({'INFO'}, "Preferences loaded")
        else:
            self.report({'WARNING'}, "No saved preferences found")
        return {'FINISHED'}


class OT_ResetAll(Operator):
    bl_idname = "trackpuck.reset_all"
    bl_label = "Reset All"

    def execute(self, context):
        reset_all_to_defaults(context.scene.trackpuck)
        self.report({'INFO'}, "Reset to defaults")
        return {'FINISHED'}


# -------------------------
# Register
# -------------------------
classes = (
    TrackpuckProperties,
    TRACKPUCK_PT_panel,
    OT_TrackpuckToggle,
    OT_SavePreferences,
    OT_LoadPreferences,
    OT_ResetAll,
)


def post_register():
    global timer_handle, libs_ready, device_connected

    log("=== Post-register: start ===")

    props = bpy.context.scene.trackpuck
    saved = load_prefs()

    if saved:
        apply_prefs_to_props(props, saved)
        log(f"Preferences loaded")
    else:
        log(f"WARNING: No saved preferences found")

    log("Post-register: Start auto-connect...")
    
    if not libs_ready:
        log("Post-register: importing libs...")
        if not import_libs():
            props.connected = False
            log(f"Post-register: import failed - {import_error}")
            # self.report({'ERROR'}, import_error or "Failed to import library")
            # return {'CANCELTED'}
        libs_ready = True
    
    log("Post-register: activating device...")
    if activate_device():
        props.connected = True
        log("Post-register: connected successfully")
        
        if not timer_handle:
            timer_handle = bpy.app.timers.register(trackpuck_timer)
        
        # self.report({'INFO'}, "Trackpuck connected")
    else:
        props.connected = False
        log("Post-register: device not found")
        # self.report({'ERROR'}, "No Trackpuck device found")

    log("=== Post-register: done ===")


def register():
    global libs_ready, device_connected
    
    log("=== register: start ===")
    
    # Reset device state (in case re-enabled after being disabled while connected)
    device_connected = False
    
    # Check config first
    if not config_valid:
        log(f"register: config invalid - {config_error}")
    else:
        log(f"register: config valid")

    # Register classes
    for cls in classes:
        bpy.utils.register_class(cls)
    
    # Create property
    bpy.types.Scene.trackpuck = PointerProperty(type=TrackpuckProperties)

    # Pull libs from PyPI if needed
    log("register: pulling libs...")
    pull_libs()
    
    # Import libs (but don't auto-connect)
    log("register: importing libs...")
    if import_libs():
        libs_ready = True
        log("register: libs ready")
    else:
        log(f"register: import failed - {import_error}")
    
    bpy.app.timers.register(post_register)
    log("=== register: done ===")


def unregister():
    global timer_handle
    
    log("=== unregister: start ===")
    
    # Deactivate device
    deactivate_device()

    # Unregister timer
    if timer_handle:
        try:
            bpy.app.timers.unregister(trackpuck_timer)
        except:
            pass
        timer_handle = None
    
    # Delete property
    del bpy.types.Scene.trackpuck
    
    # Unregister classes
    for cls in reversed(classes):
        bpy.utils.unregister_class(cls)
    
    log("=== unregister: done ===")


if __name__ == "__main__":
    register()
