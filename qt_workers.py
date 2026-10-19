# -*- coding: utf-8 -*-
from __future__ import annotations

import traceback
import re
from collections.abc import Callable
from typing import Any

from PySide6.QtCore import QObject, QRunnable, Signal, Slot
from subtitle_tool_core import CancelledError


class MuxProgressTracker:
    """UI-only state from actual tool reports, independent of batch progress.

    Concurrent movies own separate states; finishing one cannot complete the
    others. A tool reaching 100% still needs output checks and publication.
    """
    def __init__(self, name=''):
        self.name=name
        self.states={}

    def reset(self):
        self.states.clear()

    def update(self, message):
        match=re.match(r'^(.*?\.(?:mkv|mp4|mov|avi|m4v))：(.*)$',message,re.IGNORECASE)
        name,text=match.groups() if match else (self.name,message)
        state=self.states.get(name)
        if any(word in text for word in ('mkvmerge 封装开始：','正在写入修改后的译文','正在准备重新封装','转换为 MP4 输出：')):
            state=dict(percent=-1,phase='正在准备' if '正在准备重新封装' in text else '正在封装',active=True)
            self.states[name]=state
        elif state is None:
            return
        percent=re.search(r'mkvmerge 封装进度 (\d+)%',text)
        if percent:
            state['percent']=max(state['percent'],min(100,int(percent[1])))
        if 'mkvmerge 封装完成：' in text or '重新封装写入完成' in text or 'MP4 封装完成，正在验证' in text:
            state.update(percent=100,phase='正在验证')
        if '全部检查通过，正在替换成品' in text:
            state['phase']='正在替换成品'
        if '正式输出已就绪：' in text or '修改后的译文已写入成品：' in text:
            state.update(percent=100,phase='已完成',active=False)
        elif any(word in text for word in ('处理失败：','重新封装未完成：','重新封装失败：')):
            state.update(phase='未完成，原成品保留',active=False)
        elif '已停止重新封装' in text or '本片已停止' in text:
            state.update(phase='已停止',active=False)

    def finish_unpublished(self):
        for state in self.states.values():
            if state['active']: state.update(phase='未完成',active=False)

    @property
    def visible(self): return bool(self.states)

    @property
    def selected(self):
        active=[(name,state) for name,state in self.states.items() if state['active']]
        return active[0] if active else next(reversed(self.states.items()),('',{}))

    @property
    def value(self): return max(0,self.selected[1].get('percent',0))

    @property
    def busy(self):
        state=self.selected[1]
        return state.get('active',False) and (state.get('percent',-1)<0 or state.get('phase')!='正在封装')

    @property
    def label(self):
        name,state=self.selected
        if not state: return ''
        percent=state['percent']
        phase=state['phase']
        suffix=f' · {percent}%' if phase=='正在封装' and percent>=0 else ''
        count=sum(item['active'] for item in self.states.values())
        concurrent=f' · 同时封装 {count} 部' if count>1 else ''
        return f'{name} · {phase}{suffix}{concurrent}'.strip(' ·')


class WorkerSignals(QObject):
    result = Signal(object)
    error = Signal(str)
    message = Signal(str)
    progress = Signal(int, str)
    state = Signal(str, str, str)
    finished = Signal()


class FunctionWorker(QRunnable):
    """Run a callable outside the UI thread and expose Qt-safe callbacks."""

    def __init__(self, function: Callable[[WorkerSignals], Any]) -> None:
        super().__init__()
        self.function = function
        self.signals = WorkerSignals()

    @Slot()
    def run(self) -> None:
        try:
            result = self.function(self.signals)
        except CancelledError:
            # Cancellation is a normal terminal state, not a task failure.
            pass
        except Exception as exc:
            detail = "".join(traceback.format_exception_only(type(exc), exc)).strip()
            self.signals.error.emit(detail)
        else:
            self.signals.result.emit(result)
        finally:
            self.signals.finished.emit()
