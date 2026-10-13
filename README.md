# Lab-Cloud-Dev-ITKMTIL

ตัวรันสำหรับ AWS Academy Cloud Developing Labs ใช้ Terraform หรือ OpenTofu ร่วมกับ Python เพื่อสร้างและตั้งค่าทรัพยากรตามโจทย์ รวมถึงใช้ทรัพยากรที่ Lab เตรียมไว้

**Start Lab → clone → ตั้งค่า `.env` → รัน `run.py` → ยืนยันอีเมลถ้ามี → Submit**

## 1. เตรียมเครื่อง

คู่มือนี้ใช้ Linux หรือ WSL โดยต้องมี Git, Python 3.9 ขึ้นไป, pip และอินเทอร์เน็ต สำหรับ Module 2 ให้รันบน Linux/WSL

ตัวอย่างติดตั้งเครื่องมือบน Ubuntu/WSL:

```bash
sudo apt update
sudo apt install -y git python3 python3-pip
```

ตรวจสอบก่อนเริ่ม:

```bash
git --version
python3 --version
python3 -m pip --version
```

ตัวรันดาวน์โหลด Terraform หรือ OpenTofu และติดตั้ง Python dependencies ให้อัตโนมัติใน `.tools` ของ Lab ที่รัน

## 2. Clone โปรเจกต์

```bash
git clone https://github.com/13D4C/Lab-Cloud-Dev-ITKMTIL.git
cd Lab-Cloud-Dev-ITKMTIL
```

คำสั่งทั้งหมดด้านล่างให้รันจากโฟลเดอร์หลักนี้

## 3. Start Lab และตั้งค่า credentials

เปิด Lab ที่ต้องการใน AWS Academy กด **Start Lab** และรอให้พร้อม หากโจทย์กำหนดให้รอ CloudFormation ให้รอจน stack สร้างเสร็จด้วย

คัดลอก `.env.example` เป็น `.env` ในโฟลเดอร์เดียวกับ `run.py` ของ Lab นั้น ตัวอย่าง Module 3:

```bash
cp Module-3/.env.example Module-3/.env
```

หากมี `.env` อยู่แล้ว ให้แก้ไฟล์เดิม ใส่ AWS CLI credentials ของ Lab ที่กำลังเปิดใช้งาน:

```dotenv
aws_access_key_id=YOUR_AWS_ACCESS_KEY_ID
aws_secret_access_key=YOUR_AWS_SECRET_ACCESS_KEY
aws_session_token=YOUR_AWS_SESSION_TOKEN
aws_region=us-east-1
```

ต้องใส่ทั้งสามค่า รวมถึง `aws_session_token` เมื่อ credentials หมดอายุหรือเปลี่ยน Lab ให้อัปเดต `.env` ของ Lab นั้น

สำหรับโฟลเดอร์ย่อย เช่น Lab 8.1 ให้สร้างไฟล์ที่โฟลเดอร์ย่อยโดยตรง:

```bash
cp Module-8/Lab-8.1/.env.example Module-8/Lab-8.1/.env
```

## 4. รัน Lab

เลือกคำสั่งให้ตรงกับ Lab ที่เปิดใน AWS Academy:

| Module / Lab | งานหลัก | คำสั่ง |
| --- | --- | --- |
| 2 | CloudShell, VS Code IDE และ Hello World บน S3 | `python3 Module-2/run.py` |
| 3 | เว็บไซต์ S3 และ bucket policy จำกัด IP | `python3 Module-3/run.py` |
| 5 | DynamoDB และข้อมูลสินค้า | `python3 Module-5/run.py` |
| 6 | API Gateway และ mock API | `python3 Module-6/run.py` |
| 7 | Lambda, DynamoDB และ API Gateway | `python3 Module-7/run.py` |
| 8 / 8.1 | Docker และ ECR พร้อมตั้งค่าสำหรับระบบตรวจคะแนน | `python3 Module-8/Lab-8.1/run.py --prepare-grading` |
| 8 / 8.2 | Aurora และ Elastic Beanstalk | `python3 Module-8/Lab-8.2/run.py` |
| 9 / 9.1 | ElastiCache และแอป café | `python3 Module-9/Lab-9.1/run.py` |
| 9 / 9.2 | CloudFront และ WAF | `python3 Module-9/Lab-9.2/run.py` |
| 10 / 10.1 | SNS และ SQS | `python3 Module-10/Lab-10.1/run.py` |
| 11 | Step Functions และรายงานผ่าน SNS | `python3 Module-11/run.py` |
| 12 | Cognito และ API authorizer | `python3 Module-12/run.py` |
| 13 | CodeCommit และ CodePipeline | `python3 Module-13/run.py` |

Module 1 และ 4 ยังไม่มี `run.py` ในโปรเจกต์นี้

ตัวอย่างรัน Module 3:

```bash
python3 Module-3/run.py
```

รอจนโปรแกรมแจ้งว่าทำงานและตรวจสอบสำเร็จ จากนั้นใช้ URL หรือข้อมูลที่โปรแกรมแสดงเพื่อเปิดระบบของ Lab

## 5. ค่าที่ต้องตั้งเพิ่มในบาง Module

เพิ่มหรือแก้ค่าเหล่านี้ใน `.env` ข้าง `run.py` ของ Lab ที่เลือก:

| Module | ค่า | วิธีใช้ |
| --- | --- | --- |
| 3 | `bucket_prefix` | ชื่อย่อสำหรับชื่อ bucket ตั้งก่อนรันครั้งแรก ค่าเริ่มต้นคือ `cafe` |
| 3 | `site_bucket` | ระบุชื่อ bucket เอง เว้นว่างเพื่อให้โปรแกรมตั้งชื่อให้ |
| 3, 8, 9, 10, 11, 12, 13 | `client_cidr` | ระบุ public IPv4 ของเครื่องที่เปิดเว็บไซต์พร้อม `/32` เว้นว่างเพื่อให้ตรวจจากเครื่องที่รัน |
| 6, 7 | `website_source_cidr` | ระบุ IP ที่อนุญาตให้เข้าเว็บไซต์ ใช้แทน `client_cidr` ในสอง Module นี้ |
| 11, 12, 13 | `report_email` | อีเมลของตัวเองที่รับข้อความจาก AWS SNS ได้ |
| 12 | `frank_password` | รหัสผ่านผู้ใช้ `frank` เว้นว่างเพื่อให้โปรแกรมสร้างให้ |
| 13 | `commit_author` | ชื่อผู้สร้าง commit ในแบบฝึกหัด CodeCommit |

ตัวอย่างค่าเพิ่มเติมสำหรับ Module 12:

```dotenv
report_email=your-email@example.com
frank_password=
```

Module 12 เก็บบัญชี `frank` และรหัสผ่านที่สร้างไว้ใน `Module-12/.tools/login.json`

สำหรับ Lab ที่จำกัด IP ให้เปิดเว็บไซต์จากเครือข่ายเดียวกับ IP ที่อนุญาต หากเปลี่ยนเครือข่าย ให้อัปเดตค่าหรือเว้นว่างให้ตรวจใหม่ แล้วรันคำสั่งเดิม

## 6. ยืนยันอีเมล SNS และ Submit

Module 11, 12 และ 13 มีการสมัครรับอีเมล SNS ให้เปิดอีเมลจาก **AWS Notifications** และกด **Confirm subscription** โดยตรวจทั้ง Inbox และ Spam โปรแกรมจะทำต่อเมื่อยืนยันสำเร็จ

หากผลตรวจคะแนนแสดง `PendingConfirmation` หรือแจ้งว่ายังไม่ยืนยันอีเมล ให้ยืนยันให้เสร็จก่อน แล้วกด **Submit** ใหม่

เมื่อโปรแกรมแจ้งว่าทดสอบผ่าน:

1. กลับไปหน้า AWS Academy แล้วกด **Submit**
2. ตรวจผลที่ **Grades** หรือ **View Submission Report**
3. เมื่อได้ผลที่ต้องการแล้วจึงกด **End Lab**

สำหรับ Module 11 ให้เก็บ email subscription ไว้จนตรวจคะแนนเสร็จ หากต้องการยกเลิกภายหลัง ใช้:

```bash
python3 Module-11/run.py --unsubscribe-email
```

หากเคยยกเลิกแล้วและต้องการเปิดรับอีเมลใหม่:

```bash
python3 Module-11/run.py --subscribe-email
```

## 7. ตรวจระบบและรันซ้ำ

รันซ้ำใน Lab เดิมได้ด้วยคำสั่งเดิม เก็บ `.tools` และไฟล์ state ของ Lab ไว้เพื่อใช้ติดตามทรัพยากรที่จัดการแล้ว

Module 2, 3, 11, 12, 13 และ Lab 10.1 มีตัวเลือก `--verify-only` สำหรับตรวจระบบที่เคยสร้าง ตัวอย่าง:

```bash
python3 Module-3/run.py --verify-only
python3 Module-12/run.py --verify-only
```

Module 6 และ 7 ใช้คำสั่ง `verify`:

```bash
python3 Module-6/run.py verify
python3 Module-7/run.py verify
```

## แก้ปัญหาเบื้องต้น

| อาการ | วิธีแก้ |
| --- | --- |
| `ExpiredToken` หรือ credentials ใช้ไม่ได้ | คัดลอก credentials ล่าสุดของ Lab ลง `.env` แล้วรันใหม่ |
| ไม่พบ bucket, IDE หรือทรัพยากรที่คาดไว้ | ตรวจว่าเปิด Lab ตรงกับ Module ใช้ credentials ของ Lab นั้น และรอ AWS เตรียมทรัพยากรเสร็จ |
| เปิดเว็บไซต์แล้วได้ `AccessDenied` / HTTP 403 | ตรวจ IP ที่อนุญาตใน `client_cidr` หรือ `website_source_cidr` และเครือข่ายที่ใช้เปิดเว็บ |
| โปรแกรมรอยืนยันอีเมล | กด **Confirm subscription** ในอีเมล AWS Notifications |
| `No module named pip` | ติดตั้ง `python3-pip` แล้วรันใหม่ |
| หา `run.py` ไม่พบ | ตรวจชื่อโฟลเดอร์และ branch/commit ที่ clone ว่ามีไฟล์ของ Lab ที่ต้องการแล้ว |

`.env`, `.tools`, `.terraform` และไฟล์ Terraform state ถูกยกเว้นไว้ใน `.gitignore` จึงไม่รวมไปกับการ clone ของผู้อื่น แต่ละคนต้องตั้งค่า credentials ของตัวเอง
