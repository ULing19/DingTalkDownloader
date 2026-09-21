"""Exercise the real GUI layout without login/network writes."""
import sys
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
    app.update()
    names={'开始下载','停止','解析到任务列表','打开保存目录','重新登录','GitHub 仓库','检查更新'}
    buttons={w.cget('text'):w for w in descendants(app) if isinstance(w,ctk.CTkButton) and w.cget('text') in names}
    assert set(buttons)==names
    for factor in (1,1.25,1.5,1.75,2):
        ctk.set_widget_scaling(factor)
        ctk.set_window_scaling(factor)
        for width,height in ((1024,768),(1366,768),(1920,1080)):
            scale=app._get_window_scaling()
            app.minsize(1,1)
            app.geometry(f'{int((width-24)/scale)}x{int((height-80)/scale)}')
            app.update()
            W,H=app.winfo_width(),app.winfo_height()
            for name,w in buttons.items():
                x=w.winfo_rootx()-app.winfo_rootx();y=w.winfo_rooty()-app.winfo_rooty()
                assert w.winfo_ismapped() and x>=0 and y>=0 and x+w.winfo_width()<=W+1 and y+w.winfo_height()<=H+1,(factor,width,height,name,(x,y,w.winfo_width(),w.winfo_height()),(W,H))
            print('PASS',factor,width,height,flush=True)
    app.destroy()

with patch.object(ctk.CTk,'mainloop',inspect),patch.object(gui,'prepare_session_storage',side_effect=OSError('layout-only')):
    gui.build_gui()
