# dsh-plugin-qqsend 的 profile 补丁层（bundle）
#
# 这个文件由安装器 install.ps1 从本模板生成：DSH 的 bundle 补丁层是按**绝对路径**
# 挂载插件的（裸包名解析不到 profile 的 node_modules），而每台机器解压位置不同，
# 所以路径必须现场渲染。
#
# 与 dsh-plugin-qqchat（只读）**完全独立**：两个 bundle 各自启停。
#
# 占位符：
#   {{LIB}}      插件入口的绝对路径（<插件目录>\lib\index.js）
#   {{CONFIG}}   配置文件路径
- insert:
    - id: plugin-qqsend-write-1
      name: '{{LIB}}'
      config:
        configPath: '{{CONFIG}}'
