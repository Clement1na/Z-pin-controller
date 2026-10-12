/*
 * GX28 微型丝杆滑台（T6丝杆，有效行程50mm）步进控制 + 双气阀
 * 硬件：ESP32 + CNC Shield(A4988/DRV8825) + 两相步进电机 + 2个气阀
 * 依赖库：AccelStepper（Arduino IDE: 工具 -> 管理库 -> 搜 "AccelStepper" 安装）
 *
 * 串口命令（115200）：
 *   数字(如 -12 / 12.5) -> 移动到绝对位置(mm)，软件范围 -30..30
 *   a  auto             -> 执行一次自动植入流程
 *   a1 / a0             -> 气阀A 开 / 关
 *   b1 / b0             -> 气阀B 开 / 关
 *   h  home             -> 回零，移动到 0mm
 *   z  zero             -> 把当前位置定义为 0mm
 *   v  version          -> 查询上位机协议版本
 *   t  test             -> 禁用的旧 50mm 测试命令
 *   s  status           -> 打印当前位置与参数
 *   e  enable           -> 使能电机（锁住）
 *   d  disable          -> 释放电机（可手动推动）
 *   q  stop             -> 运动中紧急停止
 */

#include <AccelStepper.h>

// ===================== 1. 接线（按你实际飞线改这里）=====================
#define STEP_PIN  26   // 接 A4988 的 STEP
#define DIR_PIN   16   // 接 A4988 的 DIR
#define EN_PIN    12   // 接 CNC Shield 的 EN（若你把 A4988 的 EN 直接接地，可忽略此项）
#define VALVE_A_PIN 2 // 气阀A（经继电器/MOS驱动板，高电平=开，低电平=关）
#define VALVE_B_PIN 4 // 气阀B

// ===================== 2. 机械/驱动参数（务必按实物改）=====================
#define MOTOR_STEPS_PER_REV  200    // 28步进电机：步距角1.8° = 200步/圈（固定）
#define MICROSTEPS           16     // 看盾板上的 MS0/MS1/MS2 跳线：全插=16细分，只MS0=2，只MS1=4...
#define SCREW_LEAD_MM        1.0f   // ★ T6丝杆导程：常见 1 / 2 / 4 / 8 mm，查铭牌！
#define TRAVEL_MM            50.0f  // 有效行程 50mm
#define WORK_MIN_MM          -30.0f // 相对人工确认零位的下行软件限位，仍须确认机械余量
#define WORK_MAX_MM           30.0f // 相对人工确认零位的上行软件限位，并非硬件限位开关

// ===================== 3. 速度/加速度（可调）=====================
#define MAX_SPEED  4000.0f   // 步/s
#define ACCEL      2000.0f   // 步/s^2

// ===================== 4. 自动植入流程参数（务必按实际改）=====================
#define MOVE_DOWN_MM   5.0f    // ★ 向下移动距离(mm)
#define MOVE_UP_MM     5.0f    // ★ 向上移动距离(mm)
#define VALVE_DELAY_MS 500     // ★ 气阀/气缸动作稳定等待时间(ms)

// 每毫米需要的步数 = 每圈步数(含细分) / 导程
const float STEPS_PER_MM = MOTOR_STEPS_PER_REV * MICROSTEPS / SCREW_LEAD_MM;

AccelStepper stepper(AccelStepper::DRIVER, STEP_PIN, DIR_PIN);
bool stopLatched = false;
bool zeroConfirmed = false;

void setup() {
  Serial.begin(115200);

  pinMode(EN_PIN, OUTPUT);
  enableMotor();

  // 气阀引脚初始化，默认关闭
  pinMode(VALVE_A_PIN, OUTPUT);
  pinMode(VALVE_B_PIN, OUTPUT);
  setValveA(false);
  setValveB(false);

  stepper.setMaxSpeed(MAX_SPEED);
  stepper.setAcceleration(ACCEL);

  printHelp();
}

void loop() {
  // 处理串口命令
  if (Serial.available()) {
    String cmd = Serial.readStringUntil('\n');
    cmd.trim();
    if (cmd.length() > 0) handleCommand(cmd);
  }
  // 必须常驻调用，让电机持续走
  stepper.run();
}

void handleCommand(String cmd) {
  cmd.toLowerCase();

  if (cmd == "a" || cmd == "auto") {
    autoImplant();
  }
  else if (cmd == "a1") {
    if (!zeroConfirmed || stopLatched) Serial.println("错误: 未人工确认机械待机位并 z 对零");
    else { setValveA(true); Serial.println("气阀A 打开"); }
  }
  else if (cmd == "a0") { setValveA(false); Serial.println("气阀A 关闭"); }
  else if (cmd == "b1") {
    if (!zeroConfirmed || stopLatched) Serial.println("错误: 未人工确认机械待机位并 z 对零");
    else { setValveB(true); Serial.println("气阀B 打开"); }
  }
  else if (cmd == "b0") { setValveB(false); Serial.println("气阀B 关闭"); }
  else if (cmd == "h" || cmd == "home") {
    moveToMM(0);
  }
  else if (cmd == "z" || cmd == "zero") {
    stepper.setCurrentPosition(0);
    stopLatched = false;
    zeroConfirmed = true;
    Serial.println("当前位置已设为 0mm");
  }
  else if (cmd == "v" || cmd == "version") {
    Serial.println("ZPIN_HEAD_PROTOCOL=2");
  }
  else if (cmd == "t" || cmd == "test") {
    Serial.println("错误: 50mm 往返测试已禁用，不能用于当前机构");
  }
  else if (cmd == "q" || cmd == "stop") {
    emergencyStop();
  }
  else if (cmd == "s" || cmd == "status") {
    printStatus();
  }
  else if (cmd == "e" || cmd == "enable") {
    enableMotor();
  }
  else if (cmd == "d" || cmd == "disable") {
    disableMotor();
  }
  else if (cmd[0] == '-' || cmd[0] == '+' || cmd[0] == '.' || isDigit(cmd[0])) {
    float mm = cmd.toFloat();
    moveToMM(mm);
  }
  else {
    Serial.println("错误: 未知命令，输入 s 查看状态");
  }
}

// q 的回执仅在减速完成、双阀关闭后发出；后续动作保持锁止直至人工 z 对零。
void emergencyStop() {
  stepper.stop();
  while (stepper.distanceToGo() != 0) stepper.run();
  setValveA(false);
  setValveB(false);
  stopLatched = true;
  zeroConfirmed = false;
  Serial.println("已紧急停止！位置已不准确，请重新 z 回零");
}

bool serviceStopCommand() {
  if (!Serial.available()) return false;
  String c = Serial.readStringUntil('\n');
  c.trim();
  c.toLowerCase();
  if (c == "q" || c == "stop") {
    emergencyStop();
    return true;
  }
  Serial.println("忙碌中，仅接受 q 急停");
  return false;
}

bool waitValveDelay(int milliseconds) {
  for (int elapsed = 0; elapsed < milliseconds; elapsed += 10) {
    if (serviceStopCommand() || stopLatched) return false;
    delay(milliseconds - elapsed < 10 ? milliseconds - elapsed : 10);
  }
  return !stopLatched;
}

// ============ 自动植入流程 ============
// 1.开阀A -> 2.向下移动 -> 3.关阀A -> 4.开阀B -> 5.向上移动 -> 6.关阀B
void autoImplant() {
  if (stopLatched || !zeroConfirmed) {
    Serial.println("错误: 未人工确认机械待机位并 z 对零");
    return;
  }
  Serial.println("\n===== 开始自动植入流程 =====");

  // 1. 打开气阀A
  setValveA(true);
  Serial.println("1. 气阀A 打开");
  if (!waitValveDelay(VALVE_DELAY_MS)) return;

  // 2. 滑台向下移动
  Serial.printf("2. 滑台向下移动 %.1f mm\n", MOVE_DOWN_MM);
  if (!moveByMM(-MOVE_DOWN_MM)) {
    setValveA(false); setValveB(false);
    Serial.println("错误: 下行未完成，流程中止");
    return;
  }

  // 3. 关闭气阀A
  setValveA(false);
  Serial.println("3. 气阀A 关闭");
  if (!waitValveDelay(VALVE_DELAY_MS)) return;

  // 4. 打开气阀B
  setValveB(true);
  Serial.println("4. 气阀B 打开");
  if (!waitValveDelay(VALVE_DELAY_MS)) return;

  // 5. 滑台向上移动
  Serial.printf("5. 滑台向上移动 %.1f mm\n", MOVE_UP_MM);
  if (!moveByMM(+MOVE_UP_MM)) {
    setValveA(false); setValveB(false);
    Serial.println("错误: 上行未完成，流程中止");
    return;
  }

  // 6. 关闭气阀B
  setValveB(false);
  Serial.println("6. 气阀B 关闭");
  if (!waitValveDelay(VALVE_DELAY_MS)) return;

  Serial.println("===== 自动植入流程完成 =====\n");
}

// 气阀控制（高电平=开，低电平=关）
void setValveA(bool open) { digitalWrite(VALVE_A_PIN, open ? HIGH : LOW); }
void setValveB(bool open) { digitalWrite(VALVE_B_PIN, open ? HIGH : LOW); }

float currentPosMM() {
  return stepper.currentPosition() / STEPS_PER_MM;
}

// 相对移动 deltaMM（可正可负），最终受有符号工作窗口约束
bool moveByMM(float deltaMM) {
  return moveToMM(currentPosMM() + deltaMM);
}

// 移动到绝对位置(mm)，带有符号工作窗口限位
bool moveToMM(float targetMM) {
  if (stopLatched || !zeroConfirmed) {
    Serial.println("错误: 未人工确认机械待机位并 z 对零");
    return false;
  }
  if (targetMM < WORK_MIN_MM || targetMM > WORK_MAX_MM) {
    Serial.println("错误: 目标超出已配置工作区间，不执行运动");
    return false;
  }

  long targetSteps = (long)(targetMM * STEPS_PER_MM);
  stepper.moveTo(targetSteps);

  Serial.printf("移动到 %.2f mm (%ld 步)...\n", targetMM, targetSteps);

  // 阻塞直到到达；期间可输入 q 急停
  while (stepper.distanceToGo() != 0) {
    if (serviceStopCommand()) return false;
    stepper.run();
  }
  Serial.println("到达目标");
  return true;
}

void printStatus() {
  float posMM = stepper.currentPosition() / STEPS_PER_MM;
  Serial.printf("当前位置: %.2f mm  (步数 %ld)\n", posMM, stepper.currentPosition());
  Serial.printf("导程 %.2f mm/圈, 每毫米 %ld 步, 工作窗口 %.1f~%.1f mm\n",
                SCREW_LEAD_MM, (long)STEPS_PER_MM, WORK_MIN_MM, WORK_MAX_MM);
  Serial.printf("气阀A: %s | 气阀B: %s\n",
                digitalRead(VALVE_A_PIN) ? "开" : "关",
                digitalRead(VALVE_B_PIN) ? "开" : "关");
}

void enableMotor() {
  digitalWrite(EN_PIN, LOW);   // A4988 EN 低电平=使能
  Serial.println("电机使能");
}

void disableMotor() {
  digitalWrite(EN_PIN, HIGH);  // 高电平=释放
  zeroConfirmed = false;
  Serial.println("电机释放（可手动推动滑台）");
}

void printHelp() {
  Serial.println("\n===== GX28 丝杆滑台 + 双气阀控制 =====");
  Serial.printf("导程 %.1f mm/圈，每毫米 %ld 步，工作窗口 %.1f~%.1f mm\n",
                SCREW_LEAD_MM, (long)STEPS_PER_MM, WORK_MIN_MM, WORK_MAX_MM);
  Serial.println("命令: 数字(mm)=移动 | a=自动植入 | a1/a0=阀A开关 | b1/b0=阀B开关");
  Serial.println("      h=回软件零 | z=人工确认后设零 | v=协议版本 | s=状态 | e=使能 | d=释放 | q=急停");
}
