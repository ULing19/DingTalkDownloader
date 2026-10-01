"""Exercise the real GUI layout without login/network writes."""
import sys
import time
from pathlib import Path
from unittest.mock import patch
import customtkinter as ctk
sys.path.insert(0,str(Path(__file__).resolve().parents[1]))
import gui_downloader as gui

def descendants(w):
    for child in w.winfo_children():
        yield child
        yield from descendants(child)

def inspect(app,*args,**kwargs):
    errors=[]
    app.report_callback_exception=lambda *error: errors.append(error)
    app.update()
    names={'开始下载','停止','解析到任务列表','打开保存目录','重新登录','GitHub 仓库','检查更新'}
    buttons={w.cget('text'):w for w in descendants(app) if isinstance(w,ctk.CTkButton) and w.cget('text') in names}
    assert set(buttons)==names
    input_box=next(w for w in descendants(app) if isinstance(w,ctk.CTkTextbox))
    input_box.insert('1.0','https://n.dingtalk.com/dingding/live-room/index.html?roomId=DEMO&liveUuid=00000000-0000-0000-0000-000000000000')
    original=gui.make_task_item
    def demo_task(*args,**kwargs):
        task=original(*args,**kwargs)
        task.title='演示课程：这是一个需要自动换行并完整显示的较长课程标题，验证任务名称不会遮挡选择框和下载状态。'
        return task
    with patch.object(gui,'make_task_item',demo_task):
        buttons['解析到任务列表'].invoke()
    for factor in (1,1.25,1.5,1.75,2,2.5,3):
        ctk.set_widget_scaling(factor)
        ctk.set_window_scaling(factor)
        for width,height in ((800,600),(1024,768),(1366,768),(1920,1080),(2560,1440)):
            scale=app._get_window_scaling()
            app.minsize(1,1)
            app.geometry(f'{int((width-24)/scale)}x{int((height-80)/scale)}')
            deadline=time.monotonic()+0.45
            while time.monotonic()<deadline:
                app.update()
                time.sleep(0.01)
            assert not errors,errors
            W,H=app.winfo_width(),app.winfo_height()
            assert abs(W-(width-24))<=3 and abs(H-(height-80))<=3,('window size differs from simulated screen',factor,width,height,W,H)
            for name,w in buttons.items():
                x=w.winfo_rootx()-app.winfo_rootx();y=w.winfo_rooty()-app.winfo_rooty()
                assert w.winfo_ismapped() and x>=0 and y>=0 and x+w.winfo_width()<=W+1 and y+w.winfo_height()<=H+1,(factor,width,height,name,(x,y,w.winfo_width(),w.winfo_height()),(W,H))
                text_width=w._font.measure(name)*w._get_widget_scaling()
                assert w.winfo_width() >= text_width+12,(factor,width,height,name,'clipped button text',w.winfo_width(),text_width)
            for w in descendants(app):
                if isinstance(w,(ctk.CTkButton,ctk.CTkEntry,ctk.CTkCheckBox)) and w.winfo_ismapped():
                    x=w.winfo_rootx()-app.winfo_rootx()
                    assert x>=0 and x+w.winfo_width()<=W+1,(factor,width,height,w.cget('width'),'horizontal overflow',x,w.winfo_width(),W)
                if isinstance(w,(ctk.CTkButton,ctk.CTkLabel,ctk.CTkEntry,ctk.CTkCheckBox)) and isinstance(w.master,ctk.CTkFrame):
                    if isinstance(w,ctk.CTkLabel) and not w.cget('text'):
                        continue
                    assert w.winfo_ismapped(),(factor,width,height,str(w),'unmapped content',w.cget('text') if not isinstance(w,ctk.CTkEntry) else '',w.master.winfo_width(),w.master.winfo_height())
                    x=w.winfo_rootx()-w.master.winfo_rootx()
                    y=w.winfo_rooty()-w.master.winfo_rooty()
                    assert x>=0 and y>=0 and x+w.winfo_width()<=w.master.winfo_width()+1 and y+w.winfo_height()<=w.master.winfo_height()+1,(factor,width,height,str(w),'parent clips content',x,y,w.winfo_width(),w.winfo_height(),w.master.winfo_width(),w.master.winfo_height())
            print('PASS',factor,width,height,flush=True)
    app.destroy()

with patch.object(ctk.CTk,'mainloop',inspect),patch.object(gui,'prepare_session_storage',side_effect=OSError('layout-only')),patch.object(gui,'fetch_latest_release',side_effect=RuntimeError('layout-only')):
    gui.build_gui()
