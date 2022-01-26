import kopf
import pyvisa
import chroma
import logging


def check(params: dict, setp: dict):
    # TODO implement safety checks
    pass


def configure(amp: chroma.amp4Q, params: dict, phases: list[int], setp: dict):
    check(params, setp)

    amp.config_device(
        params.get('maxCurrent', 0),
        params.get('overcurrentDelay', 0),
        params.get('maxPower', 0),
        params.get('maxFrequency', 51),
        params.get('maxVoltageAC', 0),
        params.get('maxVoltageDCplus', 0),
        params.get('maxVoltageDCminus', 0)
    )

    for i in phases:
        amp.set_frequency(i, setp.get('frequency')[i])
        amp.set_voltage_AC(i, setp.get('voltageAC')[i])
        amp.set_voltage_DC(i, setp.get('voltageDC')[i])


@kopf.on.startup()
def startup(settings: kopf.OperatorSettings, **_):
    pyvisa.logger.setLevel(logging.INFO)


@kopf.on.create('device.riasc.eu', 'v1', 'chroma4qs')
@kopf.on.resume('device.riasc.eu', 'v1', 'chroma4qs')
def create_or_resume(spec: kopf.Spec, memo: kopf.Memo):
    conn = spec.get('connection')
    params = spec.get('parameters')
    setp = spec.get('setpoints')
    if params is None or conn is None:
        raise kopf.PermanentError('incomplete settings')

    memo.amp = chroma.amp4Q(
        conn.get('host'),
        conn.get('port'),
        conn.get('timeout'), False)

    configure(memo.amp, params, setp)


@kopf.on.update('device.riasc.eu', 'v1', 'chroma4qs')
def update(spec: kopf.Spec, memo: kopf.Memo):
    params = spec.get('parameters')
    setp = spec.get('setpoints')
    if params is None or setp is None:
        raise kopf.PermanentError('incomplete settings')

    configure(memo.amp, params, setp)


@kopf.on.delete('device.riasc.eu', 'v1', 'chroma4qs')
def delete(memo: kopf.Memo):
    memo.amp.disconnect_DUT()


@kopf.timer('device.riasc.eu', 'v1', 'chroma4qs', interval=5)
def measurements(spec: kopf.Spec, memo: kopf.Memo):
    phases = spec.get('phases')

    return {
        'frequency':     [memo.amp.meas_frequency(i) for i in phases],
        'voltageAC':     [memo.amp.meas_voltage_AC(i) for i in phases],
        'currentDC':     [memo.amp.meas_current_AC(i) for i in phases],
        'powerReal':     [memo.amp.meas_power_real(i) for i in phases],
        'powerReactive': [memo.amp.meas_power_reactive(i) for i in phases],
    }
