#!/usr/bin/env python3
"""
自动应用iOS兼容性修复脚本
"""

import os
import re
import shutil
from datetime import datetime

def backup_original_file():
    """备份原始文件"""
    original_file = "wx849_channel.py"
    backup_file = f"wx849_channel.py.backup_{datetime.now().strftime('%Y%m%d_%H%M%S')}"
    
    if os.path.exists(original_file):
        shutil.copy2(original_file, backup_file)
        print(f"✓ 已备份原始文件到: {backup_file}")
        return True
    else:
        print(f"✗ 找不到原始文件: {original_file}")
        return False

def check_files_exist():
    """检查必要文件是否存在"""
    required_files = [
        "wx849_channel.py",
        "ios_compatibility.py"
    ]
    
    missing_files = []
    for file in required_files:
        if not os.path.exists(file):
            missing_files.append(file)
    
    if missing_files:
        print(f"✗ 缺少必要文件: {', '.join(missing_files)}")
        return False
    
    print("✓ 所有必要文件都存在")
    return True

def add_import_statement():
    """添加导入语句"""
    with open("wx849_channel.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # 检查是否已经添加了导入语句
    if "from .ios_compatibility import IOSCompatibilityHandler" in content:
        print("✓ 导入语句已存在，跳过")
        return True
    
    # 找到合适的位置添加导入语句
    # 在其他import语句后添加
    import_pattern = r"(from\s+\w+.*?import.*?\n)"
    matches = list(re.finditer(import_pattern, content))
    
    if matches:
        # 在最后一个import语句后添加
        last_import = matches[-1]
        insert_pos = last_import.end()
        
        new_import = "from .ios_compatibility import IOSCompatibilityHandler\n"
        content = content[:insert_pos] + new_import + content[insert_pos:]
        
        with open("wx849_channel.py", "w", encoding="utf-8") as f:
            f.write(content)
        
        print("✓ 已添加导入语句")
        return True
    else:
        print("✗ 找不到合适的位置添加导入语句")
        return False

def add_init_code():
    """在__init__方法中添加初始化代码"""
    with open("wx849_channel.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # 检查是否已经添加了初始化代码
    if "self.ios_handler = IOSCompatibilityHandler(self)" in content:
        print("✓ 初始化代码已存在，跳过")
        return True
    
    # 找到__init__方法的结尾
    init_pattern = r"def __init__\(self\):.*?(?=\n    def|\nclass|\n@|\Z)"
    match = re.search(init_pattern, content, re.DOTALL)
    
    if match:
        init_method = match.group(0)
        # 在方法结尾添加初始化代码
        init_code = "\n        # 初始化iOS兼容性处理器\n        self.ios_handler = IOSCompatibilityHandler(self)"
        
        new_init_method = init_method + init_code
        content = content.replace(init_method, new_init_method)
        
        with open("wx849_channel.py", "w", encoding="utf-8") as f:
            f.write(content)
        
        print("✓ 已添加初始化代码")
        return True
    else:
        print("✗ 找不到__init__方法")
        return False

def add_error_detection():
    """添加错误检测代码"""
    with open("wx849_channel.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    # 检查是否已经添加了错误检测代码
    if "self.ios_handler.is_ios_error(result)" in content:
        print("✓ 错误检测代码已存在，跳过")
        return True
    
    # 找到错误检查的位置
    error_check_pattern = r'# 检查响应是否成功\s*\n\s*if not result\.get\("Success", False\):\s*\n\s*logger\.error\(f"\[WX849\] 下载图片分段失败: \{result\.get\(\'Message\', \'未知错误\'\)\}"\)\s*\n\s*all_chunks_success = False\s*\n\s*break'
    
    match = re.search(error_check_pattern, content)
    
    if match:
        old_code = match.group(0)
        
        new_code = '''# 检查响应是否成功
                            if not result.get("Success", False):
                                error_msg = result.get('Message', '未知错误')
                                logger.error(f"[WX849] 下载图片分段失败: {error_msg}")
                                
                                # 检测iOS设备特征：BaseResponse.ret = -104错误
                                if self.ios_handler.is_ios_error(result):
                                    logger.warning(f"[WX849] 检测到iOS设备-104错误，启用iOS兼容模式")
                                    # 使用iOS兼容模式下载
                                    ios_success = await self.ios_handler.download_image_ios_mode(cmsg, image_path)
                                    if ios_success:
                                        return True
                                    else:
                                        logger.error(f"[WX849] iOS兼容模式也失败了")
                                
                                all_chunks_success = False
                                break'''
        
        content = content.replace(old_code, new_code)
        
        with open("wx849_channel.py", "w", encoding="utf-8") as f:
            f.write(content)
        
        print("✓ 已添加错误检测代码")
        return True
    else:
        print("✗ 找不到错误检查代码位置")
        return False

def verify_installation():
    """验证安装是否成功"""
    with open("wx849_channel.py", "r", encoding="utf-8") as f:
        content = f.read()
    
    checks = [
        ("导入语句", "from .ios_compatibility import IOSCompatibilityHandler"),
        ("初始化代码", "self.ios_handler = IOSCompatibilityHandler(self)"),
        ("错误检测", "self.ios_handler.is_ios_error(result)")
    ]
    
    all_passed = True
    for check_name, check_code in checks:
        if check_code in content:
            print(f"✓ {check_name}验证通过")
        else:
            print(f"✗ {check_name}验证失败")
            all_passed = False
    
    return all_passed

def main():
    """主函数"""
    print("=" * 50)
    print("iOS兼容性修复自动安装脚本")
    print("=" * 50)
    
    # 检查文件是否存在
    if not check_files_exist():
        return False
    
    # 备份原始文件
    if not backup_original_file():
        return False
    
    print("\n开始应用修复...")
    
    # 应用修复
    steps = [
        ("添加导入语句", add_import_statement),
        ("添加初始化代码", add_init_code),
        ("添加错误检测代码", add_error_detection)
    ]
    
    for step_name, step_func in steps:
        print(f"\n{step_name}...")
        if not step_func():
            print(f"✗ {step_name}失败")
            return False
    
    print("\n验证安装...")
    if verify_installation():
        print("\n" + "=" * 50)
        print("✓ iOS兼容性修复安装成功！")
        print("=" * 50)
        print("\n使用说明：")
        print("1. 重启微信机器人服务")
        print("2. 使用iOS设备发送图片测试")
        print("3. 观察日志输出确认修复生效")
        print("\n如有问题，请查看 README_iOS_Fix.md")
        return True
    else:
        print("\n" + "=" * 50)
        print("✗ 安装验证失败，请手动检查")
        print("=" * 50)
        return False

if __name__ == "__main__":
    success = main()
    exit(0 if success else 1) 