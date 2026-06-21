<template id="page-zapbox">
  <div class="row q-col-gutter-md">
    <div class="col-12 col-md-8 q-gutter-y-md">
      <q-card>
        <q-card-section>
          <q-btn unelevated color="primary" @click="formDialog.show = true"
            >New ZapBox Instance
          </q-btn>
        </q-card-section>
      </q-card>

      <q-card>
        <q-card-section>
          <div class="row items-center no-wrap q-mb-md">
            <div class="col">
              <h5 class="text-subtitle1 q-my-none">ZapBox Instances</h5>
            </div>

            <div class="col-auto">
              <q-input
                borderless
                dense
                debounce="300"
                v-model="filter"
                placeholder="Search"
              >
                <template v-slot:append>
                  <q-icon name="search"></q-icon>
                </template>
              </q-input>
              <q-btn flat color="grey" @click="exportCSV">Export to CSV</q-btn>
            </div>
          </div>
          <q-table
            flat
            dense
            :rows="zapboxes"
            row-key="id"
            :columns="zapboxTable.columns"
            v-model:pagination="zapboxTable.pagination"
          >
            <template v-slot:header="props">
              <q-tr :props="props">
                <q-th auto-width></q-th>
                <q-th
                  v-for="col in props.cols"
                  :key="col.name"
                  :props="props"
                  auto-width
                >
                  <div v-text="col.label"></div>
                </q-th>
              </q-tr>
            </template>

            <template v-slot:body="props">
              <q-tr :props="props">
                <q-td style="display: flex; grid-gap: 12px">
                  <q-btn
                    flat
                    dense
                    size="xs"
                    @click="openUpdateZapBox(props.row.id)"
                    icon="edit"
                    color="blue"
                  >
                    <q-tooltip>Edit ZapBox Switch</q-tooltip>
                  </q-btn>
                  <q-btn
                    flat
                    dense
                    size="xs"
                    @click="copyDeviceString(props.row.id)"
                    icon="perm_data_setting"
                    color="primary"
                  >
                    <q-tooltip>Device string</q-tooltip>
                  </q-btn>
                  <q-btn
                    :disable="protocol == 'http:'"
                    flat
                    unelevated
                    dense
                    size="xs"
                    icon="visibility"
                    @click="openQrCodeDialog(props.row.id)"
                    ><q-tooltip v-if="protocol == 'http:'">
                      LNURLs only work over HTTPS </q-tooltip
                    ><q-tooltip v-else> view LNURLS </q-tooltip></q-btn
                  >
                  <q-btn
                    flat
                    dense
                    size="xs"
                    @click="openPublicLink(props.row.id)"
                    icon="link"
                    color="orange"
                  >
                    <q-tooltip
                      >Open public LNURL Paylink ZapBox page.</q-tooltip
                    >
                  </q-btn>
                  <span style="flex-grow: 1"></span>
                  <q-btn
                    flat
                    dense
                    size="xs"
                    @click="deleteZapBox(props.row.id)"
                    icon="cancel"
                    color="pink"
                  >
                    <q-tooltip>Delete ZapBox Switch</q-tooltip>
                  </q-btn>
                </q-td>
                <q-td
                  v-for="col in props.cols"
                  :key="col.name"
                  :props="props"
                  auto-width
                >
                  <div v-if="col.name == 'id'"></div>
                  <div v-else v-text="col.value"></div>
                </q-td>
              </q-tr>
            </template>
          </q-table>
        </q-card-section>
      </q-card>
    </div>

    <div class="col-12 col-md-4 q-gutter-y-md">
      <q-card>
        <q-card-section>
          <h6 class="text-subtitle1 q-my-none">
            LNbits Zap&#9889;Box Extension
          </h6>
        </q-card-section>
        <q-separator></q-separator>
        <q-card-section>
          <p>
            Turn on and off things with bitcoin!<br />
            Web installer:
            <a
              class="text-secondary"
              href="https://installer.zapbox.space"
            >
              https://installer.zapbox.space</a
            ><br />
            Main page:
            <a
              class="text-secondary"
              href="https://zapbox.space"
            >
              https://zapbox.space</a
            ><br />
            Documentation:
            <a
              class="text-secondary"
              href="https://ereignishorizont.xyz/en/zapbox-en/"
            >
              https://ereignishorizont.xyz/zapbox</a
            ><br />
            Hardware:
            <a
              class="text-secondary"
              href="https://github.com/AxelHamburch/ZapBox"
            >
              https://github.com/AxelHamburch/ZapBox</a
            ><br />
            Extension:
            <a
              class="text-secondary"
              href="https://github.com/AxelHamburch/zapbox_extension"
            >
              https://github.com/AxelHamburch/zapbox_extension</a
            ><br />
            <small>
              Created by,
              <a class="text-secondary" href="https://github.com/benarc"
                >Ben Arc</a
              >,
              <a class="text-secondary" href="https://github.com/blackcoffeexbt"
                >BC</a
              >,
              <a class="text-secondary" href="https://github.com/motorina0"
                >Vlad Stan</a
              >,
              <a class="text-secondary" href="https://github.com/dni">dni</a>,
              <a class="text-secondary" href="https://github.com/AxelHamburch">AxelHamburch</a>
            </small>
          </p>
        </q-card-section>
      </q-card>
    </div>

    <q-dialog
      v-model="formDialog.show"
      deviceition="top"
      size="md"
      @hide="closeFormDialog"
    >
      <q-card class="q-pa-lg q-pt-xl">
        <q-form @submit="sendFormData" class="q-gutter-md">
          <h5 v-html="formDialog.data.device" v-if="formDialog.data.id"></h5>
          <q-input
            filled
            dense
            v-model.trim="formDialog.data.title"
            type="text"
            label="Title"
          ></q-input>
          <q-select
            filled
            dense
            emit-value
            v-model="formDialog.data.wallet"
            :options="g.user.walletOptions"
            label="Wallet *"
          ></q-select>
          <q-select
            filled
            dense
            v-model.trim="formDialog.data.currency"
            type="text"
            :label="$t('currency')"
            :options="currencies"
          ></q-select>
          <q-input
            filled
            dense
            v-model.trim="formDialog.data.password"
            type="text"
            label="Password (optional)"
            tooltip="Password to protect the device, needs to be entered as comment in the LNURL"
          ></q-input>
          <q-btn
            unelevated
            class="q-mb-lg"
            round
            size="sm"
            icon="add"
            @click="addSwitch"
            v-model="formDialog.data.switches"
            color="primary"
          ></q-btn>
          <q-btn
            unelevated
            class="q-mb-lg"
            round
            size="sm"
            icon="remove"
            @click="removeSwitch"
            v-model="formDialog.data.switches"
            color="primary"
          ></q-btn>
          <q-toggle
            v-model="formDialog.data.disabled"
            color="primary"
            :label="
              formDialog.data.disabled
                ? 'Switch is disabled'
                : 'Switch is enabled'
            "
            dense
            class="q-mt-none q-ml-lg"
            ><q-tooltip
              >Disable the device, it will return lnurl errors.</q-tooltip
            ></q-toggle
          >
          <q-toggle
            v-model="formDialog.data.disposable"
            color="primary"
            :label="
              formDialog.data.disposable
                ? 'Paylink is disposable (LUD11)'
                : 'Paylink is stored (LUD11)'
            "
            dense
            class="q-mt-none q-ml-lg"
            ><q-tooltip
              >If disabled signals LUD11 disposable and will be stored if the
              wallet supports it.</q-tooltip
            ></q-toggle
          >
          <div v-for="_switch in formDialog.data.switches">
            <div class="row">
              <div class="col">
                <q-input
                  ref="setAmount"
                  filled
                  dense
                  v-model.trim="_switch.amount"
                  class="q-pb-md"
                  :label="'Amount (' + formDialog.data.currency + ') *'"
                ></q-input>
              </div>
              <div class="col q-ml-md">
                <q-input
                  filled
                  dense
                  v-model.trim="_switch.duration"
                  type="number"
                  label="duration (ms)"
                ></q-input>
              </div>
              <div class="col q-ml-md">
                <q-input
                  filled
                  dense
                  v-model.trim="_switch.pin"
                  type="number"
                  label="GPIO pin"
                ></q-input>
              </div>
              <div class="col q-ml-md">
                <q-input
                  filled
                  dense
                  v-model.trim="_switch.label"
                  type="text"
                  label="Label"
                ></q-input>
              </div>
              <div class="col q-ml-md">
                <q-checkbox
                  v-model="_switch.variable"
                  color="primary"
                  label="Variable"
                  size="xs"
                  dense
                  ><q-tooltip
                    >Variable time (Amount * Duration)</q-tooltip
                  ></q-checkbox
                >
                <q-checkbox
                  v-model="_switch.comment"
                  color="primary"
                  label="Comment"
                  size="xs"
                  dense
                >
                  <q-tooltip>Enable LNURLp comments with payments</q-tooltip>
                </q-checkbox>
              </div>
            </div>
          </div>
          <q-separator
            v-if="formDialog.data.id"
            class="q-mt-md"
          ></q-separator>
          <div v-if="formDialog.data.id" class="q-mt-md">
            <div class="row items-center q-mb-sm">
              <div class="col text-subtitle2">Option: Identities (LNURL-auth)</div>
              <q-btn
                flat
                dense
                round
                size="sm"
                icon="refresh"
                color="primary"
                @click="getAuthKeys(formDialog.data.id)"
              >
                <q-tooltip>Reload identities</q-tooltip>
              </q-btn>
            </div>
            <q-toggle
              v-model="formDialog.data.auth_enabled"
              color="primary"
              :label="
                formDialog.data.auth_enabled
                  ? 'Identities (LNURL-auth) ENABLED'
                  : 'Identities (LNURL-auth) DISABLED'
              "
              dense
              class="q-mb-sm"
              ><q-tooltip
                >Master switch for the whole LNURL-auth feature. When off, the
                device cannot identify wallets or teach — all /auth endpoints
                refuse.</q-tooltip
              ></q-toggle
            >
            <q-toggle
              v-model="formDialog.data.touch_enabled"
              color="primary"
              :disable="!formDialog.data.auth_enabled"
              :label="
                formDialog.data.touch_enabled
                  ? 'Touch teach mode is ENABLED'
                  : 'Touch teach mode is DISABLED (locked)'
              "
              dense
              class="q-mb-sm"
              ><q-tooltip
                >Gate for enrolling new identities. Auto-disabled after 3 wrong
                Teach-PINs; re-enable here. Normal payments are unaffected.</q-tooltip
              ></q-toggle
            >
            <q-input
              filled
              dense
              v-model.trim="formDialog.data.teach_pin"
              type="text"
              inputmode="numeric"
              maxlength="6"
              label="Teach-PIN (6 digits)"
              class="q-mb-sm"
            >
              <q-tooltip
                >6-digit PIN entered on the device (6 taps + hold) to open the
                teach mode. Verified server-side.</q-tooltip
              >
            </q-input>
            <div v-if="authKeys.length === 0" class="text-caption text-grey">
              No identities enrolled yet. Open the teach mode on the device
              (6 taps + hold) and scan the register QR with a wallet.
            </div>
            <q-list v-else bordered separator dense>
              <q-item v-for="ak in authKeys" :key="ak.id">
                <q-item-section>
                  <q-input
                    dense
                    borderless
                    v-model.trim="ak.label"
                    placeholder="Label"
                    @blur="updateAuthKey(ak)"
                  ></q-input>
                  <q-item-label caption
                    >{{ shortKey(ak.pubkey) }} ·
                    {{ formatDate(ak.created_at) }}</q-item-label
                  >
                </q-item-section>
                <q-item-section side>
                  <q-toggle
                    v-model="ak.enabled"
                    color="primary"
                    dense
                    @update:model-value="updateAuthKey(ak)"
                  >
                    <q-tooltip>Enabled / disabled</q-tooltip>
                  </q-toggle>
                </q-item-section>
                <q-item-section side>
                  <q-btn
                    flat
                    dense
                    size="xs"
                    icon="cancel"
                    color="pink"
                    @click="deleteAuthKey(ak)"
                  >
                    <q-tooltip>Delete identity</q-tooltip>
                  </q-btn>
                </q-item-section>
              </q-item>
            </q-list>
          </div>
          <div class="row q-mt-lg">
            <q-btn
              v-if="formDialog.data.id"
              unelevated
              color="primary"
              :disable="formDialog.data.title == ''"
              type="submit"
              >Update ZapBox Instance</q-btn
            >
            <q-btn
              v-else
              unelevated
              color="primary"
              :disable="formDialog.data.title == ''"
              type="submit"
              >Create ZapBox Instance</q-btn
            >
            <q-btn @click="cancelFormDialog" flat color="grey" class="q-ml-auto"
              >Cancel</q-btn
            >
          </div>
        </q-form>
      </q-card>
    </q-dialog>
    <q-dialog v-model="qrCodeDialog.show" position="top">
      <q-card v-if="qrCodeDialog.data" class="q-pa-lg lnbits__dialog-card">
        <lnbits-qrcode-lnurl
          :url="activeUrl"
          @update:lnurl="v => (lnurl = v)"
        ></lnbits-qrcode-lnurl>
        <div class="row q-mb-sm q-gutter-sm">
          <q-btn
            v-for="_switch in qrCodeDialog.data.switches"
            outline
            color="primary"
            @click="activePin = _switch.pin"
            :disabled="_switch.pin == activePin"
            :label="switchLabel(_switch)"
          ></q-btn>
        </div>
        <div class="row">
          <q-btn v-close-popup flat color="grey">Close</q-btn>
          <q-btn outline color="grey" @click="triggerPin" class="q-ml-auto">
            <q-tooltip
              >Trigger the GPIO pin with duration via the websocket as if a
              payment hit</q-tooltip
            >
            Trigger Pin</q-btn
          >
        </div>
      </q-card>
    </q-dialog>
  </div>
</template>
