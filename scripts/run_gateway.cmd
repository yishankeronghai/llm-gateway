@echo off
chcp 65001 >nul
rem 一键启动网关（双击运行即可）。窗口保持打开 = 服务在跑；关掉窗口 = 服务停止。
cd /d "%~dp0.."

if not exist ".venv\Scripts\python.exe" (
    echo [错误] 没找到 .venv，请先创建虚拟环境并安装依赖
    pause
    exit /b 1
)

echo ============================================================
echo   启动 LLM Gateway ...  浏览器打开 http://127.0.0.1:8080
echo   停止服务：按 Ctrl+C 或直接关掉这个窗口
echo ============================================================
echo.

if "%DEEPSEEK_API_KEY%"=="" (
    echo [提示] DEEPSEEK_API_KEY 没有设置，真实模型调用会失败（实验脚本仍可用 mock）
    echo        临时设置： set DEEPSEEK_API_KEY=sk-xxxx
    echo        永久设置： setx DEEPSEEK_API_KEY "sk-xxxx"  ^(需要重开窗口^）
    echo.
)

.venv\Scripts\python.exe src\main.py
echo.
echo [服务已退出] 按任意键关闭窗口
pause >nul
