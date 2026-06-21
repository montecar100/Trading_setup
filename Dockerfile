FROM python:3.11-slim
WORKDIR /app
# 先装依赖 (层缓存), 再拷代码
COPY requirements.txt .
RUN pip install --no-cache-dir -r requirements.txt
COPY . .
# /data 用于持久化策略元数据 (entry_z / ticket); Northflank 挂卷到此
ENV STATE_DIR=/data
RUN mkdir -p /data
# 唯一入口: brain_fx.py (旧 brain.py 已废弃删除)
CMD ["python", "-u", "brain_fx.py"]
