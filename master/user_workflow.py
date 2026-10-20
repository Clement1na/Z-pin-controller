"""Compact hardware-only setup for the user GUI; the debug wizard is unchanged."""
from __future__ import annotations

from contextlib import contextmanager
from copy import deepcopy
from pathlib import Path
import math
import threading

from master.model import DEFAULT_CUT_CLEARANCE_MM, HEAD_PRE_DESCENT_MM, PresetSpec
from master.local_config import local_path_for, write_local
from master.planner import plan_at_x
from master.touch_off import SECTION, make_record, read_record, restore_position
from master.wizard import WizardCancelled, _build_motion, _local_sections, _apply, preset_values
from master.regions import TubeJobParameters, ThreeRegionJobSource


def _check_cancel(event):
    if event.is_set():
        raise WizardCancelled('操作员请求停止')


@contextmanager
def _setup_guard(devices, event, menu):
    """Setup motions need the same immediate stop path as an active job."""
    done = threading.Event()
    stopped = threading.Event()
    stop_lock = threading.Lock()
    def halt():
        event.set()
        with stop_lock:
            if stopped.is_set():
                return
            stopped.set()
        for label, device, method in (
                ('滑台停止', devices.head, 'stop'), ('CNC暂停', devices.cnc, 'hold'),
                ('伺服释放', devices.servo, 'disable')):
            if device.connected:
                try:
                    getattr(device, method)()
                except Exception as exc:
                    menu.warn(f'{label}未确认：{exc}')
    def watch():
        while not done.wait(0.01):
            if event.is_set():
                halt()
                return
    monitor = threading.Thread(target=watch, daemon=True)
    monitor.start()
    try:
        _check_cancel(event)
        yield
        _check_cancel(event)
    except BaseException:
        halt()
        raise
    finally:
        done.set()
        monitor.join()


def _head_at(head, target):
    position = head.position_mm()
    if not head.zero_confirmed or not math.isfinite(position) or abs(position-target) > 0.05:
        raise RuntimeError(f'GX28未到已确认位置 {target:g} mm，未继续动作')


def run_user_setup(config_path, *, menu, prepared_config=None, profile_store=None,
                   profile_selection=None, command_reader=None, results_dir=None):
    from master import master as cli
    from master.user_interfaces import prepare_user_interfaces
    path = Path(config_path)
    selection = profile_selection if profile_selection is not None else {}
    devices = None
    event = (getattr(command_reader, 'cancel_event', None)
             or getattr(menu, 'cancel_event', None) or threading.Event())
    try:
        cfg = (deepcopy(prepared_config) if prepared_config is not None else
               prepare_user_interfaces(path, menu, store=profile_store, selection=selection))
        parameters = menu.job_parameters(cfg.get('job', {}), regions=cfg.get('regions', {}))
        if not isinstance(parameters, TubeJobParameters):
            raise ValueError('作业参数缺少已确认的三区间距，未连接设备')
        row, layout = parameters.row, parameters.layout
        geometry = cli._known_geometry()
        source = ThreeRegionJobSource(layout)
        plan = source.plan(row, geometry)
        motion = _build_motion(row, DEFAULT_CUT_CLEARANCE_MM)
        if cli.threading_from_config(cfg.get('cycle', {})):
            cli.validate_threading_motion(motion)
        ports = [cfg.get(section, {}).get('port') for section in ('cnc3d', 'head')]
        if not all(ports):
            raise ValueError('自动植入需要CNC和GX28串口，请先完成接口配置')
        if ports[0].casefold() == ports[1].casefold():
            raise ValueError('CNC和GX28不能使用同一个串口')
        try:
            saved = read_record(cfg, row, plan, head_clearance_travel=True)
        except ValueError as exc:
            menu.warn(f'已保存对刀点不能复用：{exc}')
            saved = None
        reuse = False
        if saved is not None:
            xyz, saved_motion, _ = saved
            reuse = menu.choose(
                f'已保存对刀点 X={xyz[0]:g} Y={xyz[1]:g} Z={xyz[2]:g} mm。'
                '仅在工件、针组和CNC工作坐标基准未变化时复用。',
                (('reuse', '使用已保存对刀点'), ('new', '重新对刀')), default='new') == 'reuse'
            if reuse:
                motion = saved_motion
        cfg['job'] = preset_values(row)
        cfg['regions'] = layout.as_config()
        write_local(local_path_for(path), cfg)
        devices = cli._open_live_devices(cfg, geometry, motion)
        for device in (devices.head, devices.cnc, devices.servo):
            device.cancel_event = event
        with _setup_guard(devices, event, menu):
            cli._connect_live_devices(devices)
            _check_cancel(event)
            if not menu.ask_yes_no(
                    '确认GX28位于开机人工机械零位、下方有15 mm余量，针尖离开工件；'
                    '两针已安装且针距10 mm，连接伺服时须处于靠紧零位。'
                    + ('确认后恢复保存的位置。' if reuse else '确认后设零并向下准备15 mm。'), default=False):
                return 1
            _check_cancel(event)
            devices.head.zero()
            devices.head.enable()
            _head_at(devices.head, 0)
            if devices.servo.connected:
                try:
                    devices.servo.enable()
                    devices.servo.set_zero()
                except Exception as exc:
                    _check_cancel(event)
                    menu.warn(f'伺服初始化失败：{exc}；未自动调整针距，请人工核对')
            if reuse:
                touch_off, motion, _ = saved
                restore_position(devices.cnc, devices.head, touch_off, motion, touch_off[2],
                                 destination_z=touch_off[2], surface_slope=row.slope)
            else:
                verify = getattr(devices.head, 'verify_work_window', None)
                if verify is not None:
                    verify(motion)
                devices.head.valve('a', False)
                devices.head.valve('b', False)
                devices.head.motion = motion
                _check_cancel(event)
                devices.head.move_mm(motion.standby_mm)
                _head_at(devices.head, motion.standby_mm)
                while True:
                    if not menu.ask_yes_no(
                            '请手动完成CNC对刀：以已装两针中的左针为排头，针尖刚好接触管面。'
                            '完成后确认，程序记录XYZ并将GX28上抬15 mm。', default=False):
                        return 1
                    _check_cancel(event)
                    touch_off = devices.cnc.position()
                    if touch_off is not None and len(touch_off) == 3 and all(math.isfinite(v) for v in touch_off):
                        touch_off = tuple(touch_off)
                        break
                    menu.warn('读不到有效CNC工作坐标，请检查连接后重试')
                _head_at(devices.head, motion.standby_mm)
                devices.head.move_mm(motion.cut_position_mm)
                _head_at(devices.head, motion.cut_position_mm)
            _check_cancel(event)
            datum = (touch_off[0], touch_off[2])
            plan = plan_at_x(plan, touch_off[0])
            env = {section: dict(cfg.get(section, {})) for section in ('servo', 'cnc3d', 'head')}
            _apply(cfg, _local_sections(env, motion, touch_off[2]))
            cfg['head']['cut_safe_mm'] = motion.cut_position_mm
            write_local(local_path_for(path), cfg)
            cfg, row, _, motion, scan_z, plan, datum = cli._read_live_configuration(
                path, True, preset_values(row), datum=datum, row_y_mm=touch_off[1],
                row_x_mm=touch_off[0], head_clearance_travel=True, job_source=source)
            cli._print_plan(row, plan, datum=datum, emit=menu.say)
            decision = menu.confirm_run(
                f'3个区，每区{row.count}根，架角{row.angle_deg:g}°，深度{row.depth_mm:g} mm，'
                f'滑台行程{row.slide_travel_mm:.3f} mm。\n'
                f'区间空白沿管{layout.gap_mm:g} mm；三个区共{len(plan.placements)}次动作为一排。\n'
                f'对刀 X={touch_off[0]:g} Y={touch_off[1]:g} Z={touch_off[2]:g} mm。\n'
                'GX28抬针15 mm并确认到位后才移动；每次剪丝后继续，三区完成回第一区等待转管。',
                can_save=not reuse)
            _check_cancel(event)
            if not reuse and decision.get('save_touch_off'):
                cfg[SECTION] = make_record(cfg, row, touch_off, motion, scan_z)
            write_local(local_path_for(path), cfg)
            if profile_store is not None and selection.get('machine'):
                profile_store.save('machines', selection['machine'],
                                   {k: v for k, v in cfg.items() if k != 'custom'})
            if not decision.get('start'):
                menu.say('已保存本次设置，未开始植入')
                return 0
        return cli._execute_live_job(cfg, row, motion, scan_z, plan, devices, datum=datum,
                    row_y_mm=touch_off[1], command_reader=command_reader,
                    results_dir=Path(results_dir) if results_dir is not None else path.parent/'runs',
                    head_clearance_travel=True)
    except WizardCancelled as exc:
        menu.warn(f'已取消：{exc}')
        return 1
    finally:
        if devices is not None:
            cli._close_live_devices(devices)
